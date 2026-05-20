---
phase: 03-handler-processor-on-fork
plan: 13
subsystem: dogfood
tags: [dogfood, dog-05, author-push, activation-invariant, wf-12, force-push, eject]
dependency-graph:
  requires:
    - "rocm_mq.dogfood._base (plan 03-10 — DogfoodResult dataclass + create_dogfood_pr + post_command + poll_pr_state + emit_result)"
    - "rocm_mq.decision activation-state-machine (Phase 2 — pr.is_validly_active gate; eject reason 'activation invalid (branch updated or label tampered)' is the verbatim string emitted from decision.py)"
    - "tests/gh_fake.py FakeGitHub + FakePR (Phase 2 — head_sha mutation seam used by the test extension)"
  provides:
    - ".github/merge-queue/src/rocm_mq/dogfood/dog_05.py — DOG-05 driver (author-push-between-activation-and-squash eject scenario; WF-12 path)"
    - ".github/merge-queue/tests/test_dogfood_dog_05.py — 12 unit tests covering happy path, three failure modes, D-04 schema, head-SHA-mutation invariant, two-commit-on-branch invariant, and timeline vocabulary"
  affects:
    - "Plan 03-16 dogfood aggregator (consumes the per-run JSON dog_05 emits to dogfood-runs/)"
    - "Phase 5 porting-prep — keep this driver in the upstream-bound copy (the WF-12 invariant it exercises is RFC-mandated, not fork-specific)"
tech-stack:
  added: []
  patterns:
    - "Two-poll driver shape (activation observation poll + post-push eject poll) — first driver in the suite to split the polling phase. Each poll receives the same timeout budget (TIMEOUT_S=900s) so the live operator's worst-case wall time is bounded at 2x TIMEOUT_S, but typical activations complete in ~9 min and ejects in ~6 min so single-budget runs are the norm."
    - "Author-push via githubkit Contents API on the PR branch (create_or_update_file_contents) — same identity/credentials as the operator's GITHUB_TOKEN, no local checkout, no separate git push tooling. Reproduces from any CI runner."
    - "Test fixture mutates FakePR.head_sha on the second commit to a known PR branch — matches the live API behavior where any commit to a branch advances HEAD. The mutation is the test seam that arms the eject predicate."
key-files:
  created:
    - ".github/merge-queue/src/rocm_mq/dogfood/dog_05.py"
    - ".github/merge-queue/tests/test_dogfood_dog_05.py"
  modified: []
decisions:
  - "EXPECTED.reason is the verbatim RFC §6 + decision.py literal 'activation invalid (branch updated or label tampered)' (NOT a substring derived at runtime, unlike DOG-03). Rationale: this string is RFC-defined, not config-driven — the production code emits it from a single literal in decision.py, and a drift in either the driver or decision.py must surface as a passed=False signal rather than be papered over by a substring match. The predicate compares the full literal; failure_mode_when_reason_mismatches test guards against a regression that loosens this to a substring."
  - "Activation is observed via the mq:active label (rest.pulls.get -> labels), NOT via the merge-queue/active commit status. Rationale: the label flip is the single user-visible activation signal and the only one the PR-level pulls.get call carries cheaply; the commit status would require a second statuses call per poll. For the driver's purpose (knowing 'activation has happened, now push'), either signal suffices and the label is simpler. The processor's eject decision still keys on the commit status absence on the new head SHA — the driver does not need to inspect that to verify the documented eject."
  - "Author-push uses a NEW file path (not an update to the seed file). Rationale: the Contents API requires the prior file's blob sha for an UPDATE write, and threading that through would add a second contents.get call per scenario; CREATE on a new path needs no prior-sha. Both produce identical head-SHA-mutation behavior on the live API."
  - "Branch-name resolution prefers pulls.get(...).head.ref but falls back to the fake's _created_files attribute. Rationale: the FakeGitHub fake's _create_pull stub does not populate head.ref today (it only populates head.sha — see gh_fake.py:_PullsNS.get); rather than amend the fake fixture this driver carries a test-only fallback that inspects the last recorded branch. Live mode never hits the fallback because githubkit's real response carries head.ref. A targeted alternative — adding head.ref to the fake — was rejected to keep this plan's blast radius scoped to dog_05.py + its tests."
  - "Task 2 (checkpoint:human-verify — live fork run) is DEFERRED per the executor's objective ('Do NOT actually run live — unit tests only'). Mirrors plans 03-09 / 03-11 / 03-12's identical deferral. The structural correctness of the driver is established by the 12-test unit suite + the plan's automated <verify> block (module constants); the operator's live fork run belongs to the post-deployment runbook and will produce an addendum to this SUMMARY."
metrics:
  duration: "~30 minutes (context-load + RED + GREEN + ruff/mypy gates + SUMMARY)"
  completed: 2026-05-20
---

# Phase 3 Plan 13: dog_05.py — Summary

Authored `.github/merge-queue/src/rocm_mq/dogfood/dog_05.py`, the DOG-05 dogfood driver that exercises the RFC §6 "author push between activation and squash" eviction path. Demonstrates WF-12 (force-push / author-push invalidates activation) via the cleaner author-push flow: the driver creates a PR at an opted-in path, posts `/merge`, polls until the `mq:active` label appears (activation succeeded — `merge-queue/active` commit status is now pinned to the activation-time head SHA), then pushes a new commit to the PR branch via githubkit's Contents API, then polls until the processor's next cycle ejects with the verbatim reason `"activation invalid (branch updated or label tampered)"`. Two-mode driver mirroring plans 03-11 (`dog_02`) and 03-12 (`dog_03`): `run_scenario(client, ...)` for unit-tested orchestration via a `_DogfoodFake` FakeGitHub extension; `main()` for `python -m rocm_mq.dogfood.dog_05 --owner ... --repo ...` against the live fork (operator-initiated, not exercised in CI per the executor's objective). Per-run JSON emission to `.planning/phases/03-handler-processor-on-fork/dogfood-runs/` honors the D-04 schema verbatim.

## What Shipped

- **`.github/merge-queue/src/rocm_mq/dogfood/dog_05.py`** (new, 481 lines) — module docstring referencing RFC §6 / decision.py / WF-12 / CONTEXT.md D-04; constants `SCENARIO_ID="dog_05"`, `EXPECTED={"action": "Eject", "reason": "activation invalid (branch updated or label tampered)"}`, `TIMEOUT_S=900` (15 min from PATTERNS.md timeout-budget table), `_SEED_PATH_PREFIX` + `_PUSH_PATH_PREFIX` both under `projects/hipdnn/` (opted-in path); helpers `_build_active_label_predicate()` (polls `rest.pulls.get` → inspects labels for `mq:active`, captures the head SHA at activation), `_build_eject_predicate()` (polls `rest.issues.list_comments` for a comment carrying both the `rocm-mq-status` marker and the verbatim eject-reason literal), `_push_author_commit_to_branch()` (Contents API create on a NEW file path — no UPDATE-blob-sha threading); `run_scenario()` orchestrates create-PR → /merge → poll-activation → resolve-branch → push-author-commit → re-read-head-sha → poll-eject → emit JSON; `main()` is the live-fork CLI entrypoint (lazy githubkit import, GITHUB_TOKEN gating, return-code 0/1/2 contract). Timeline records the 8-event subset called for by plan 03-13's `<action>` block: `pr_opened`, `merge_command_posted`, `mq_queued_label_applied`, `activation_began`, `merge_queue_active_status_posted`, `mq_active_label_applied`, `author_push_after_activation` (custom event_type per plan), `ejected` (with the documented reason).
- **`.github/merge-queue/tests/test_dogfood_dog_05.py`** (new, 498 lines, 12 tests) — module constants (SCENARIO_ID, EXPECTED-verbatim-literal, TIMEOUT_S=900, main+run_scenario callability); happy-path orchestration (activation observed → push → eject with verbatim reason → passed=True, expected_outcome matches EXPECTED exactly, JSON emitted with passed=True); D-04 schema completeness (all 12 fields present in the emitted JSON); two-commit-on-branch invariant (the seed commit and the author-push commit go to the SAME branch at DIFFERENT paths); head-SHA mutation invariant (after the push, FakePR.head_sha differs from the activation-time SHA — guards against a regression where the push step degenerates to a no-op); failure mode 1 (eject with a different reason like `"merge conflict with develop"` → TimeoutError, no false-positive); failure mode 2 (activation never happens — driver times out at the activation poll and the author-push step never fires, guards against a regression that pushes before activation); timeline vocabulary (`author_push_after_activation` event_type present + carries the post-push head SHA in its observed_state for audit-trail completeness).

## Commits

| Hash | Kind | Subject |
|------|------|---------|
| `16ed69f58bf` | test | test(03-13): add failing tests for dog_05 author-push-after-activation driver (RED gate) |
| `e137c59d5f6` | feat | feat(03-13): implement dog_05 author-push-after-activation eject driver (GREEN gate) |

## Verification

Plan's `<verify><automated>` block ran clean against the GREEN commit:

```
$ cd .github/merge-queue && python3 -c "from rocm_mq.dogfood import dog_05; \
    assert dog_05.SCENARIO_ID == 'dog_05'; \
    assert dog_05.EXPECTED['reason'] == 'activation invalid (branch updated or label tampered)'; \
    assert dog_05.TIMEOUT_S == 900"
# (exit 0, no output — assertions held)
```

Unit suite for this plan (12 tests, all green):

```
$ python3 -m pytest tests/test_dogfood_dog_05.py -v
... 12 passed in 0.33s
```

Full project regression (482 tests, all green — no existing test regressed under the new driver + test addition):

```
$ python3 -m pytest tests/ -q
... 482 passed in 53.28s
```

Lint + type gates (per CLAUDE.md `ruff` + `mypy --strict` on decision layer; this is I/O layer so non-strict):

```
$ ruff check src/rocm_mq/dogfood/dog_05.py tests/test_dogfood_dog_05.py
All checks passed!

$ ruff format --check src/rocm_mq/dogfood/dog_05.py tests/test_dogfood_dog_05.py
... 2 files already formatted

$ mypy src/rocm_mq/dogfood/dog_05.py
Success: no issues found in 1 source file
```

## Deviations from Plan

None substantive. Two minor refinements documented below are within Rule 1/2 auto-fix scope.

- **[Rule 2 - Critical robustness] Branch-name resolution fallback.** Plan `<action>` says to push to "the PR branch" without specifying the resolution method. The FakeGitHub fake's `_PullsNS.get` does NOT populate `head.ref` (only `head.sha`), so the natural `pulls.get(...).head.ref` read returns the empty string in unit tests. The driver now reads `head.ref` first and falls back to inspecting the fake's `_created_files` attribute when empty, ensuring the test seam works without amending the shared fake. Live mode always satisfies `head.ref` and never hits the fallback. Alternative considered: add `head.ref` to `FakeGitHub._PullsNS.get` — rejected to keep this plan's blast radius scoped to `dog_05.py` + its tests.
- **[Rule 2 - Hygiene] Pre-existing-file collision avoidance.** Plan suggests `dogfood-author-push-RANDOMHASH-extra.txt` as the author-push path; implementation uses `projects/hipdnn/dogfood-author-push-extra-{8-hex}.txt`. The hex suffix is shared with the seed path so concurrent runs of dog_05 stay disjoint on BOTH writes, not just the seed. No semantic change.

## Self-Check: PASSED

- `[ -f .github/merge-queue/src/rocm_mq/dogfood/dog_05.py ]` → FOUND
- `[ -f .github/merge-queue/tests/test_dogfood_dog_05.py ]` → FOUND
- `git log --oneline --all | grep -q 16ed69f58bf` → FOUND (RED commit)
- `git log --oneline --all | grep -q e137c59d5f6` → FOUND (GREEN commit)
- `python3 -m pytest tests/test_dogfood_dog_05.py` → 12 passed
- Full suite regression → 482 passed
- ruff check + format → clean
- mypy → clean

## Outstanding Live Verification

Plan Task 2 (`checkpoint:human-verify`) — the live-fork run of `python -m rocm_mq.dogfood.dog_05 --owner SamuelReeder --repo rocm-libraries` — is DEFERRED per the executor's objective. Acceptance criteria reproduced here for the operator's runbook:

1. With `GITHUB_TOKEN` exported, run: `python -m rocm_mq.dogfood.dog_05 --owner SamuelReeder --repo rocm-libraries`.
2. Observe: PR opened → `/merge` posted → `mq:queued` then `mq:active` appear (after ≤9 min, 2-3 cron cycles) → driver pushes new commit (visible in PR timeline as a fresh commit on the dogfood/dog_05-* branch) → ≤3 more cron cycles later the eject status comment appears containing the literal `"activation invalid (branch updated or label tampered)"`.
3. Driver exits 0; per-run JSON appears in `.planning/phases/03-handler-processor-on-fork/dogfood-runs/` with `passed: true`.
4. Verify the PR's eject status comment body contains the verbatim RFC §6 reason string.

When this runs, append an `## Outstanding Live Verification — Resolved` section to this SUMMARY with the PR URL, the cycle URL, the per-run JSON filename, and the head SHA the processor rejected (the value of `post_push_head_sha` in the `author_push_after_activation` timeline event).
