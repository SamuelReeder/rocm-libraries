---
phase: 02-i-o-layer-executor
plan: 02
subsystem: infra
tags: [io-adapter, snapshot, gh-fake, contract-tests, simpleuser-bridging, pure-09]

# Dependency graph
requires:
  - phase: 02-i-o-layer-executor
    plan: 01
    provides: GitHubClient (.rest passthrough), AppIdentity dataclass, CorruptSquashError, PURE-09 positive lint, githubkit dep pinned
provides:
  - build_snapshot(client, config, owner, repo) -> RawSnapshot — I/O adapter that fans out per-PR fetches and assembles frozen dataclasses
  - _make_status_creator(creator, config) -> CommitStatusCreator — SimpleUser → CommitStatusCreator bridging (Critical Discovery 1)
  - _map_check_run_state(status, conclusion) -> str — UK-7 mapping table
  - _make_raw_pr_state(pr, statuses, timeline, checks, files, config) -> RawPRState — pure assembly helper
  - FakeGitHub / FakeRepoState / FakePR — in-memory GitHub substitute for IO-06 contract testing
  - 27 new tests (17 snapshot unit + 10 contract) pinning fake/real parity for four load-bearing semantics
affects: [02-03 executor, 02-04 cmd_process]

# Tech tracking
tech-stack:
  added: []  # no new runtime deps; reuses githubkit/httpx already pinned in 02-01
  patterns:
    - "Duck-typed mock pattern — tests construct SimpleNamespace stand-ins for githubkit Pydantic models, keeping unit tests decoupled from githubkit version churn"
    - "Owner/repo as explicit build_snapshot kwargs (vs MergeQueueConfig field) — avoids widening Phase 1 dataclass + 5 test construction sites"
    - "FakeRequestFailed aliased to real RequestFailed (constructed via __new__) so executor except clauses behave identically against fake and real"
    - "Activation status context (merge-queue/active) excluded from required_check_results aggregation — control-plane signal, not a CI check"
    - "Non-frozen dataclass deliberately for FakePR / FakeRepoState — only exception to the frozen-dataclass rule, scoped to tests/ only"

key-files:
  created:
    - ".github/merge-queue/src/rocm_mq/snapshot.py"
    - ".github/merge-queue/tests/test_snapshot.py"
    - ".github/merge-queue/tests/gh_fake.py"
    - ".github/merge-queue/tests/test_io_contract.py"
    - ".planning/phases/02-i-o-layer-executor/02-02-SUMMARY.md"
  modified: []

key-decisions:
  - "OQ-2 resolved: build_snapshot SKIPS pulls.list_files when any mq:<queue> label is already present on the PR. Labels are the source of truth post-handler. Documented as a precondition in the build_snapshot docstring."
  - "OQ-4 resolved: required_check_results combines BOTH check runs (Checks API) AND commit statuses (legacy status API), so external CI driving via status posts is covered alongside GHA-native check runs."
  - "Owner/repo as explicit build_snapshot keyword args, NOT new MergeQueueConfig fields. Trade-off: avoids touching 5 existing MergeQueueConfig construction sites; price is an extra parameter at the I/O boundary call sites (executor + cmd_process in later plans)."
  - "FakeRequestFailed alias: the public name maps to githubkit.exception.RequestFailed itself, constructed via __new__ + attribute injection (matches test_gh_client.py). Subclassing was not necessary; the duck-typed shim catches identically in the executor's except clauses."
  - "Activation status context excluded from required_check_results — it is a control-plane signal evaluated separately by derive_pr's activation filter. Including it would double-count the activation as a CI check and break the head-of-all-queues invariant."

requirements-completed: [IO-02, IO-06]

# Metrics
duration: ~25min
completed: 2026-05-19
---

# Phase 02 Plan 02: Snapshot I/O Adapter + GitHub Fake + Contract Tests Summary

**Built ``build_snapshot`` (the I/O adapter translating githubkit responses into the frozen ``RawSnapshot`` consumed by Phase 1's pure ``derive_snapshot``) plus the in-memory ``FakeGitHub`` substitute and contract tests that lock fake/real parity for the four load-bearing GitHub semantic behaviors.**

## Performance

- **Duration:** ~25 min
- **Completed:** 2026-05-19
- **Tasks:** 3
- **Files created:** 5 (1 src, 3 tests, 1 SUMMARY)
- **Commits:** 5 (1 test [RED snapshot] + 1 feat [GREEN snapshot] + 1 test [skeleton gh_fake RED] + 1 feat [GREEN gh_fake] + 1 test [contract tests])

## Accomplishments

- **IO-02 landed.** ``rocm_mq.snapshot.build_snapshot`` reads every PR carrying an ``mq:<queue>`` label across all configured queues, fans out per-PR fetches (pull, statuses, timeline, checks, optionally files), assembles frozen ``RawPRState`` entries through ``_make_raw_pr_state``, and returns a single ``RawSnapshot``. The function asserts ``incomplete_results == False`` on every search response (T-02-02-02 mitigation — the stateless processor retries on the next 3-min cron tick rather than committing to a silently truncated PR set).
- **Critical Discovery 1 implemented.** ``_make_status_creator`` bridges the API's ``SimpleUser``-shaped status creator (no ``app_id``/``app_slug`` fields) to the pure-layer's ``CommitStatusCreator``. App identity is inferred from BOTH ``type == "Bot"`` AND ``login == f"{slug}[bot]"`` exactly — sibling workflow bots (``github-actions[bot]``) and User-type creators get ``app_slug=None, app_id=None`` so ``is_app_identity`` rejects them by missing-field, not by slug equality (Pitfall 2 family). The two threat tests (``__wrong_login__`` and ``__user_type__``) lock this behavior.
- **IO-06 contract tests landed.** ``test_io_contract.py`` runs 10 tests pinning the four load-bearing GitHub semantics on ``FakeGitHub``: (1) (sha, context) status overwrite, (2) label add idempotency, (3) ``remove_label`` 404 when absent, (4) ``repos.merge`` 204-vs-201 status codes. Plus a 2-test app-vs-workflow creator bridging pair (T-02-02-01 / T-02-02-05 verification). The parametrized fixture is wired so a future nightly CI job can flip on ``"real"`` parametrization with one line + a ``GITHUB_TOKEN`` env secret.
- **FakeGitHub stays out of production.** ``FakeRepoState`` / ``FakePR`` / ``FakeGitHub`` all live in ``tests/`` (never imported from ``src/``). The PURE-09 positive lint extension already added in Plan 02-01 keeps the boundary on ``snapshot.py`` itself enforced; ``gh_fake.py`` is excluded from the lint by location.
- **PURE-09 positive lint extended.** The ``test_io_modules_do_import_githubkit`` parametrized case for ``snapshot`` now PASSES (was previously skipped — file did not exist). ``executor`` remains the only skipped case until Plan 02-03 lands.

## OQ Resolutions

### OQ-2: ``pulls.list_files`` skipped when ``mq:<queue>`` labels present

``build_snapshot`` checks each fetched PR's labels for any ``mq:<queue>`` entry (where ``<queue>`` is in ``config.all_queues``). When at least one such label exists, ``pulls.list_files`` is NOT called and ``RawPRState.changed_paths`` is ``()`` for that PR. Rationale: labels are the source of truth once applied (RFC §4.2); the queue assignment was made by an earlier handler cycle. Refetching the file list adds API budget cost without affecting any decision-layer output (``derive_pr`` reads queues from labels, not from paths, for already-labelled PRs).

Tests pinning this behavior:
- ``test_build_snapshot__skips_list_files_when_queue_labels_present`` — wires the mock so ``list_files`` raises if called; assertion succeeds when it isn't.
- ``test_build_snapshot__calls_list_files_when_no_queue_labels`` — verifies the inverse path is reachable for new (unlabelled) PRs.

Documented as a precondition in the ``build_snapshot`` docstring (Behaviour notes section).

### OQ-4: ``required_check_results`` combines BOTH check runs AND commit statuses

``_make_raw_pr_state`` reads both the Checks API (``checks.list_for_ref``) and the Commit Status API (``repos.list_commit_statuses_for_ref``) and merges entries into a single ``required_check_results`` tuple. Check runs are translated via ``_map_check_run_state`` from ``(status, conclusion)`` to the pure-layer state vocabulary; status entries use their existing state literal directly (pending/success/failure/error — already in the pure-layer vocabulary).

The activation status context (``config.activation_status_context``, default ``"merge-queue/active"``) is intentionally EXCLUDED from this aggregation — it is a control-plane signal evaluated separately by ``derive_pr``'s activation filter, not a CI check that should affect head-of-all-queues calculations.

Tests pinning this behavior:
- ``test_build_snapshot__combines_check_runs_and_commit_statuses`` — wires both a check run AND a commit status; asserts both names appear in the final ``required_check_results``.

## ``LabeledIssueEvent.performed_via_github_app`` field findings

Confirmed present on githubkit 0.15.5's ``LabeledIssueEvent`` (fields list: ``id, node_id, url, actor, event, commit_id, commit_url, created_at, performed_via_github_app, label``). The field is ``Optional[Integration]``. **Not currently used** by ``_make_raw_pr_state`` — the timeline-event identity check happens downstream in the pure layer via ``is_app_identity_actor(actor, expected)``, which checks ``actor.type == "Bot" AND actor.user_id == expected.bot_user_id``. ``performed_via_github_app`` is reserved as defense-in-depth for a future plan if the bot-user-id check ever needs corroboration.

## Owner/repo plumbing decision

The plan offered two options: add ``owner``/``repo`` to ``MergeQueueConfig`` OR pass them as explicit ``build_snapshot`` arguments. **Chose explicit arguments** (``build_snapshot(client, config, *, owner, repo)``).

Rationale:
- ``MergeQueueConfig`` is currently constructed at 5 sites (``tests/conftest.py``, ``tests/test_derive.py``, ``tests/test_decide_cycle_unit.py``, ``tests/test_pathmap.py``). Adding required fields would touch all five.
- The Phase 1 dataclass is a stable interface that the pure decision layer reads; widening it for I/O-only metadata creates a "config field used by only one consumer" smell.
- The price (one extra kwarg at I/O call sites) is paid only by ``cmd_process.py`` (Plan 02-04) and any future I/O entry point — far smaller blast radius than the test-file churn.

This is a clean cut at the I/O/pure boundary: the pure layer's config has pure-layer fields only; the I/O entry points take repo coordinates as explicit args.

## Task Commits

1. **Task 1 (RED): Failing tests for snapshot.build_snapshot** — `cbdfc82126b` (test)
2. **Task 1 (GREEN): Implement snapshot.build_snapshot + creator bridging** — `489c737accb` (feat) — also updates ``test_snapshot.py`` for ruff I001 fix flagged after writing the implementation
3. **Task 2 (RED): gh_fake skeleton — NotImplementedError stubs** — `5ca18b10620` (test)
4. **Task 2 (GREEN): Implement gh_fake.py in-memory GitHub fake** — `c3ae839dafd` (feat)
5. **Task 3: Contract tests for four semantic behaviors** — `01afa6d85e9` (test) — Task 3 RED and GREEN merged into a single commit because the fake was already complete; the tests were written and pass on first run.

## Files Created/Modified

### Created

- ``.github/merge-queue/src/rocm_mq/snapshot.py`` — ``build_snapshot``, ``_make_status_creator``, ``_map_check_run_state``, ``_make_raw_pr_state``, ``_make_timeline_actor``, ``_has_queue_label``. ~260 LOC incl. module docstring + per-function docstrings. PURE-09 compliance marker: explicit ``import githubkit.exception`` at module level.
- ``.github/merge-queue/tests/test_snapshot.py`` — 17 unit tests covering creator bridging (4), check-run state mapping (5), and build_snapshot integration (8 across OQ-2, OQ-4, timeline lag, search incompleteness, dedup, direct ``_make_raw_pr_state`` assembly).
- ``.github/merge-queue/tests/gh_fake.py`` — ``FakeGitHub``, ``FakeRepoState``, ``FakePR``, ``_make_request_failed``, plus ``_ReposNS``/``_IssuesNS``/``_PullsNS``/``_SearchNS``/``_ChecksNS``/``_AppsNS``/``_UsersNS`` sub-namespaces. ~440 LOC. ``FakeRequestFailed`` aliased to real ``RequestFailed``.
- ``.github/merge-queue/tests/test_io_contract.py`` — 10 contract tests pinning the four semantic behaviors + 2 creator-identity tests.

### Modified

None — the choice to pass ``owner``/``repo`` as ``build_snapshot`` kwargs avoided modifying ``state.py``, ``conftest.py``, and the four existing ``MergeQueueConfig`` construction sites.

## Decisions Made

- **Owner/repo as kwargs vs MergeQueueConfig fields:** Chose kwargs. See "Owner/repo plumbing decision" above.
- **FakeRequestFailed as alias to real RequestFailed:** The duck-typed shim (constructed via ``__new__`` + attribute injection) was simpler than a subclass and gives identical behavior under the executor's ``except RequestFailed: if e.response.status_code == 404:`` pattern.
- **Activation status context excluded from required_check_results:** Including it would double-count the activation as a CI check and corrupt the head-of-all-queues calculation in the pure decision layer.
- **Duck-typed SimpleNamespace mocks instead of real githubkit models in tests:** Keeps the unit tests decoupled from githubkit's Pydantic schema (which changes between API versions); the I/O adapter only reads attribute names, not types.
- **Contract test parametrize list hard-coded to ``["fake"]``:** The real-API path is structured but inactive. Flipping it on is a one-line change here plus a ``GITHUB_TOKEN`` env-scoped secret in the nightly workflow when ready.

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 3 — Blocking issue] ruff I001 on `test_snapshot.py` and `test_io_contract.py`**

- **Found during:** Task 1 GREEN gate (ruff check after writing the test file)
- **Issue:** Newly-created test files had import blocks ruff considered un-sorted (third-party ``rocm_mq.*`` adjacency to first-party ``tests.*``).
- **Fix:** Ran ``ruff check --fix`` to apply the standard sort; no semantic change.
- **Files modified:** ``.github/merge-queue/tests/test_snapshot.py``, ``.github/merge-queue/tests/test_io_contract.py``
- **Verification:** ``ruff check`` on each file exits 0; tests still pass.
- **Committed in:** ``489c737accb`` (snapshot) and ``01afa6d85e9`` (contract), bundled with the test files themselves.

---

**Total deviations:** 1 auto-fixed (ruff lint format)
**Impact on plan:** Cosmetic. No scope creep.

Pre-existing ruff I001 violation in ``tests/test_gh_client.py`` (from Plan 02-01) was observed but **NOT** fixed — out of scope per the executor's scope-boundary rule (file not touched by this plan).

## Issues Encountered

- **None.** All three tasks executed straight through; full suite went from 228 → 256 passing tests with no regressions.

## User Setup Required

None. The ``GITHUB_TOKEN``-gated real-API contract test path is documented but not wired; activating it is a future-plan task tied to setting up the nightly sandbox-repo CI job.

## Next Phase Readiness

- **Ready for Plan 02-03 (executor.py).** ``build_snapshot`` produces ``RawSnapshot`` exactly matching the shape ``derive_snapshot`` consumes. ``FakeGitHub`` provides the executor's test substrate: status overwrite, label idempotency, remove-404, merge 204/201 are all covered.
- **Ready for Plan 02-04 (cmd_process).** The full read→decide→execute layering is now implementable end-to-end: ``GitHubClient`` (Plan 02-01) → ``build_snapshot`` (this plan) → ``derive_snapshot`` (Phase 1) → ``decide_cycle`` (Phase 1) → executor (Plan 02-03) → render summary (Phase 1).
- **No blockers.** ruff (new files), mypy (default non-strict on I/O layer), and the 256-test suite are green.

## Self-Check: PASSED

- File ``.github/merge-queue/src/rocm_mq/snapshot.py`` — FOUND
- File ``.github/merge-queue/tests/test_snapshot.py`` — FOUND
- File ``.github/merge-queue/tests/gh_fake.py`` — FOUND
- File ``.github/merge-queue/tests/test_io_contract.py`` — FOUND
- Commit ``cbdfc82126b`` — FOUND (test Task 1 RED)
- Commit ``489c737accb`` — FOUND (feat Task 1 GREEN)
- Commit ``5ca18b10620`` — FOUND (test Task 2 RED / skeleton)
- Commit ``c3ae839dafd`` — FOUND (feat Task 2 GREEN)
- Commit ``01afa6d85e9`` — FOUND (test Task 3 contract tests)

---
*Phase: 02-i-o-layer-executor*
*Completed: 2026-05-19*
