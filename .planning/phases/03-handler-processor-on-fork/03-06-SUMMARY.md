---
phase: 03-handler-processor-on-fork
plan: 06
subsystem: command-handler
tags: [wf-01, wf-02, wf-03, wf-08, wf-11, wf-12, dog-08, cmd-handle, io-layer]
requires:
  - 03-01  # argparse subparser refactor; run_handle stub to replace
  - 03-02  # config.SELF_BOOTSTRAP_PATHS + config.load_from_develop
  - 03-03  # path_to_queues.yml shape (consumed via _load_config)
  - 03-04  # preflight pattern (mirror for run_handle delegation shape)
provides:
  - rocm_mq.cmd_handle module (main, parse_commands, is_self_bootstrap)
  - rocm_mq.cmd_handle._handle_merge / _handle_dequeue dispatch
  - rocm_mq.cmd_handle._check_perm + _check_at_enqueue_gates helpers
  - cmd_process.run_handle live dispatch (replaces plan 03-01 stub)
  - FakeGitHub gate-surface extensions (perm/reviews/combined-status/reactions)
affects:
  - .github/merge-queue/src/rocm_mq/cmd_handle.py (NEW)
  - .github/merge-queue/src/rocm_mq/cmd_process.py (run_handle wiring)
  - .github/merge-queue/tests/test_cmd_handle.py (NEW, 44 tests)
  - .github/merge-queue/tests/test_cmd_process.py (stub test → dispatch test)
  - .github/merge-queue/tests/gh_fake.py (gate-surface additions)
tech-stack:
  added: []
  patterns:
    - "fnmatch-based glob intersection for SELF_BOOTSTRAP_PATHS rejection"
    - "per-line ^/(merge|dequeue)\\s*$ regex (Area #6 default)"
    - "RequestFailed 404 → role 'none' with PR-author override (RFC §4.4)"
    - "single-comment multi-gate failure (one rejection per /merge, not one per gate)"
    - "deferred client/config builders monkeypatch-able from tests"
key-files:
  created:
    - .github/merge-queue/src/rocm_mq/cmd_handle.py
    - .github/merge-queue/tests/test_cmd_handle.py
    - .planning/phases/03-handler-processor-on-fork/03-06-SUMMARY.md
  modified:
    - .github/merge-queue/src/rocm_mq/cmd_process.py
    - .github/merge-queue/tests/test_cmd_process.py
    - .github/merge-queue/tests/gh_fake.py
decisions:
  - "Auto-resolved Task 1 PRE-CONFIRM (no user gate): ack UX = status comment + eyes-reaction; parser = per-line exact match. Both are CONTEXT.md option-a defaults already declared in the plan's must_haves.truths."
  - "Token discipline: handler accepts a single token (App installation) for Phase 3. Both label-write and comment-write are within the App's RFC §4.9 issues: write scope; a future workflow refactor may split the two via per-job tokens without changing the handler's public surface."
  - "/dequeue does NOT re-run the at-enqueue perm check (RFC §4.5 is silent on the boundary); deferred to Phase 4 if tightening is needed."
  - "Task 3 consolidated into Task 2's RED phase: TDD ordering meant the test file shipped before cmd_handle.py; 44 tests cover every behavior block (≥ Task 3's ~30 floor) — no additional Task-3 test file was needed."
metrics:
  duration: "~75 minutes (single-session sequential executor)"
  completed: 2026-05-19
---

# Phase 3 Plan 06: cmd_handle.py — `/merge` and `/dequeue` Handler Summary

WF-01, WF-02, WF-03, WF-08, WF-11 (status comment lifecycle on `/merge`)
shipped in a new `rocm_mq.cmd_handle` module wired through the existing
`cmd_process.run_handle` subcommand entrypoint. WF-12 is passively satisfied
by Phase 2's executor (no new code here; documented below).

## What Shipped

### `rocm_mq.cmd_handle` (NEW — 530 lines)

Phase 3 plan-03-06's marquee deliverable. Public surface:

| Function | Purpose | Test Coverage |
|---|---|---|
| `main(argv)` | CLI entrypoint; reads `$GITHUB_EVENT_PATH`, dispatches by command | full integration tests (idempotency, rejection, happy path, dequeue, event-skip, error paths) |
| `parse_commands(body)` | Per-line `^/(merge\|dequeue)\s*$` regex | 9 edge-case tests (Area #6 matrix) |
| `is_self_bootstrap(paths)` | fnmatch intersection with `config.SELF_BOOTSTRAP_PATHS` | 5 tests (hits, misses, mixed) |

Private helpers (each independently tested):
- `_check_perm` — live `repos.get_collaborator_permission_level` + RFC §4.4 PR-author override. Treats 404 as `role='none'` (matches the real API's behavior for non-collaborators).
- `_check_at_enqueue_gates` — checks all four WF-02 gates together and returns the list of failed gate names (so the rejection comment lists every failure in one shot, not one comment per gate).
- `_apply_labels` — single `add_labels` round-trip with `mq:queued` + every `mq:<queue>` from the derived queue set.
- `_remove_mq_labels` — per-label `remove_label` over the current `mq:*` set with 404 tolerated (idempotent dequeue).
- `_upsert_status_comment` — builds a `RenderContext` for the given state (`queued` / `ejected`), renders via `comment.render_status_body` (which embeds the `<!-- rocm-mq-status -->` marker), and upserts via `executor._find_status_comment_id` (paginated per WR-03).
- `_post_eyes_reaction` — single API call; the reactions API returns 200/201 on dup/new (no pre-check needed per RESEARCH.md Area #7).
- `_load_config` — translates `config.load_from_develop`'s raw YAML payload into a `MergeQueueConfig` (sorted longest-prefix-first to match the pathmap contract). Monkeypatched in tests to avoid the Contents API round-trip.

### `/merge` Step Order (load-bearing)

```
0. Read PR  (labels + author)
1. Idempotency short-circuit (WF-03)
   └─ mq:queued or mq:active present → eyes + comment upsert ONLY
2. Self-bootstrap rejection (WF-08 / RFC §8)
   └─ runs BEFORE any state mutation; no labels on rejection
3. Live perm check (WF-01 / RFC §4.4)
   └─ NEVER author_association (Pitfall 17)
4. Queue derivation (pulls.list_files → pathmap.queues_for_paths)
   └─ empty set → DOG-08 rejection comment
5. At-enqueue gates (WF-02)
   └─ all four gates checked together; one rejection comment lists every failure
6. Success: label apply + status comment upsert + eyes-reaction
```

The order is deliberate: idempotency short-circuits BEFORE self-bootstrap so a second `/merge` on a queued PR that happens to also touch a self-bootstrap path still ack's with eyes-reaction (the first `/merge` was the authoritative gate; the PR is already queued for whatever it touches).

### `/dequeue` Behavior

Removes every `mq:*` label currently on the PR, upserts the status comment to the `ejected` state with reason `/dequeue requested by commenter`, posts the eyes-reaction. Does NOT re-run the at-enqueue perm check (RFC §4.5 is silent on the dequeue perm boundary; Phase 4 may tighten this).

### Wiring (`cmd_process.run_handle` replacement)

The plan 03-01 stub:
```python
raise NotImplementedError("rocm_mq handle: plan 03-06 wires cmd_handle.main ...")
```
is now:
```python
from rocm_mq.cmd_handle import main as _handle_main
return _handle_main([f"--repo={args.repo}", f"--event-path={args.event_path}"])
```
Exit codes (0 handled / 1 traceback / 2 usage) are preserved verbatim. The flag re-serialization mirrors `run_preflight`'s pattern so both entrypoints stay independently usable.

### FakeGitHub Extensions

`tests/gh_fake.py` gained the API surfaces cmd_handle's gates consume (no production code uses these — they live in `tests/` per the IO-06 boundary):

| Addition | Drives |
|---|---|
| `state.collaborators: dict[str, str]` | seed username → role for perm checks |
| `state.combined_statuses: dict[sha, ...]` | seed required-check state for the gate |
| `state.reactions_log: list[(comment_id, content)]` | assert eyes-reaction posting |
| `state.comments_store: dict[pr, {id: body}]` | assert status-comment body + marker |
| `FakePR.user_login` / `.maintainer_can_modify` / `.reviews` | seed PR shape for `pulls.get` + gates |
| `_ReposNS.get_collaborator_permission_level` | live perm check |
| `_ReposNS.get_combined_status_for_ref` | required-check gate |
| `_PullsNS.list_reviews` | ≥1 approval gate |
| `_PullsNS.get` extended to include `user.login` + `maintainer_can_modify` | perm override + gate input |
| `_IssuesNS.create_comment` / `update_comment` persist bodies | marker scan + body assertion |
| `_ReactionsNS.create_for_issue_comment` | eyes-reaction |

Pre-existing 75 executor / io_contract / cmd_process tests continue to pass with these extensions (no API-shape regression).

## WF-12 Passive Coverage

Plan requirements list WF-12 ("force-push invalidates activation via missing App-created status on new head SHA") among the WF-* this plan satisfies. **cmd_handle does NOT implement WF-12 directly.** Instead, WF-12 is satisfied passively by Phase 2's executor:

- When a contributor force-pushes to a PR that already carries `mq:active`, GitHub mints a brand-new head SHA.
- The merge-queue `App`-created `merge-queue/active` commit status was bound to the *previous* head SHA (RFC §4.3).
- On the next 3-minute processor cycle, Phase 2's `derive_pr` checks the current head SHA for an App-created `merge-queue/active` status. The new SHA carries no such status → `is_validly_active = False`.
- `decide_cycle` sees the PR labeled `mq:active` but not validly active → emits an `Eject` action with reason "activation invalidated".

The relevant code surfaces (no Phase 3 changes touch these):
- `rocm_mq/decision.py` `derive_pr` — applies the creator-filter (`is_app_identity`) + context-filter to the head-SHA status list.
- `rocm_mq/executor.py` `_handle_eject` — clears `mq:active` + `mq:queued` labels and overwrites the activation status.

cmd_handle's role in WF-12 is simply to **not interfere**: a second `/merge` on a force-pushed PR (which still carries `mq:active` until the next cycle) hits the idempotency short-circuit and posts eyes-reaction only — no labels are re-applied, no perm check is re-run. The processor's next cycle then ejects the PR via the normal flow, and the contributor can re-post `/merge` cleanly afterwards.

## Deviations from Plan

### Auto-resolved Decisions

**1. [Task 1 — PRE-CONFIRM] Ack UX + parser strictness defaults locked.**
- **Resolution:** option-a (CONTEXT.md defaults — status comment + eyes-reaction on trigger comment + per-line exact-match `^/(merge|dequeue)\s*$` regex).
- **Why auto-resolved:** The plan's `must_haves.truths` already declares both as defaults ("ack UX is status-comment + eyes-reaction... CONTEXT.md Discretion default"; "command parsing is per-line exact match... CONTEXT.md Discretion default"). The execution-context guidance explicitly authorizes auto-resolution when the decision is technically forced by earlier-plan choices.
- **Alternatives considered:** option-b (RFC-minimal, no reaction — rejected: silent second `/merge` produces no visible feedback); option-c (stricter parsing with fenced-block tracking — rejected: significant additional complexity for a vanishingly rare paste pattern).

### Task 3 Consolidation

The plan separates Task 2 (implement cmd_handle.py with TDD) and Task 3 (add tests/test_cmd_handle.py). TDD ordering means the test file SHIPS BEFORE the implementation file (RED → GREEN). My RED-phase test file (`tests/test_cmd_handle.py`, committed in 3f9e97bef00) already covers every behavior block in Task 2 across 44 tests (≥ Task 3's "~30+" floor). No additional Task-3-only tests were needed; this is documented here rather than treated as a deviation.

### No Other Deviations

The plan's `<interfaces>` Phase 2 surfaces list was consumed verbatim. No new Phase 2 helpers were introduced. The `cmd_handle.py` import block matches the PATTERNS.md `### cmd_handle.py` block. The token-split discipline is honored (a single App-token-backed client serves both write surfaces because both fall within the App's `issues: write` scope per RFC §4.9; the audit-friendly two-client shape stays available for a future split).

## Authentication Gates

None encountered. The implementation runs entirely against `FakeGitHub` in tests; the live App-token path is exercised only when the workflow (plan 03-07) ships and runs against the real fork.

## Threat Surface Scan

No new threat surface beyond the plan's `<threat_model>` register. T-03-06-01 (commenter spoofing) is mitigated by `_check_perm`'s live API call; T-03-06-02 (path_to_queues tampering) is mitigated by `_load_config` reading from `develop` ref; T-03-06-03 (self-bootstrap path EoP) is mitigated by `is_self_bootstrap` running BEFORE any state mutation; T-03-06-04 (inline-code `/merge` misfire) is mitigated by the `^/` regex rejecting backtick-prefixed lines (covered by `test_inline_code_does_not_match`); T-03-06-06 / T-03-06-07 (idempotent second `/merge`) are mitigated by the explicit short-circuit (covered by three idempotency tests).

T-03-06-08 (WF-12 passive defense via Phase 2 executor) is documented above. No new mitigation work was added on Phase 3's side.

## Verification

| Check | Result |
|---|---|
| `pytest tests/test_cmd_handle.py` | 44 passed |
| `pytest tests/test_cmd_process.py` | 23 passed (including renamed `test_main_handle_dispatches_to_module`) |
| `pytest tests/` (full suite) | 399 passed, 8 snapshots passed |
| `pytest tests/test_pure_layer_imports.py` | 60 passed (cmd_handle correctly NOT in PURE_LAYER_MODULES) |
| `python -c "from rocm_mq.cmd_handle import main, parse_commands, is_self_bootstrap"` | OK |
| `grep -c "_CMD_RE" cmd_handle.py` | 2 (>= 2 required) |
| `ruff check src/rocm_mq/ tests/` | All checks passed |
| `mypy src/rocm_mq/cmd_handle.py src/rocm_mq/cmd_process.py` | Success: no issues found in 2 source files |

## Commits

| Hash | Message |
|---|---|
| `3f9e97bef00` | `test(03-06): add failing tests for cmd_handle + FakeGitHub gate surfaces` (RED) |
| `41d0ef03af7` | `feat(03-06): implement cmd_handle.py for /merge and /dequeue dispatch` (GREEN) |
| `5b284474545` | `feat(03-06): wire cmd_process.run_handle to rocm_mq.cmd_handle.main` (Task 4) |

## Known Stubs

None. The implementation is complete — every code path the plan called for is wired (no `pass` stubs, no `raise NotImplementedError` in the public surface). The one `NotImplementedError` in `cmd_handle._build_default_config` is for a code path the module never reaches at runtime (callers always go through `_load_config`); it exists for API-symmetry with `cmd_process._build_default_config` and a defensive guard against a future refactor that bypasses `_load_config`.

## Self-Check: PASSED

- `.github/merge-queue/src/rocm_mq/cmd_handle.py` — FOUND
- `.github/merge-queue/tests/test_cmd_handle.py` — FOUND
- Modified `.github/merge-queue/src/rocm_mq/cmd_process.py` — FOUND (run_handle replaced)
- Modified `.github/merge-queue/tests/test_cmd_process.py` — FOUND (test renamed)
- Modified `.github/merge-queue/tests/gh_fake.py` — FOUND (gate surfaces added)
- Commit `3f9e97bef00` — FOUND
- Commit `41d0ef03af7` — FOUND
- Commit `5b284474545` — FOUND
