---
phase: 03-handler-processor-on-fork
plan: 15
subsystem: dogfood
tags: [dogfood, dog-07, approval-revoked, rfc-section-5, defense-in-depth, second-account, eject]
dependency-graph:
  requires:
    - "rocm_mq.dogfood._base (plan 03-10 — DogfoodResult dataclass + create_dogfood_pr + post_command + poll_pr_state + emit_result)"
    - "tests/gh_fake.py FakeGitHub + FakePR + FakeRepoState (Phase 2 — reviews list on FakePR consumed by the at-enqueue gate; plan 03-15 adds dismiss_review/create_review on a local FakeGitHub extension, not on the shared fake)"
    - "RFC §5 + §6 (defense-in-depth: approval-revocation property; documented eject reason 'approval revoked')"
  provides:
    - ".github/merge-queue/src/rocm_mq/dogfood/dog_07.py — DOG-07 driver (approval-revoked eject scenario; RFC §5 defense-in-depth)"
    - ".github/merge-queue/tests/test_dogfood_dog_07.py — 14 unit tests covering happy path, three failure modes, D-04 schema, approval-then-dismiss ordering invariant, APPROVER_TOKEN env-var gate, and timeline vocabulary"
    - "PRE-CONFIRM Task 1 outcome — option-a (second collaborator account + PAT) is the recorded mechanism; APPROVER_TOKEN env var is the contract"
  affects:
    - "Plan 03-16 dogfood aggregator (consumes the per-run JSON dog_07 emits to dogfood-runs/)"
    - "Plan 03-16 DOGFOOD-RESULTS.md prerequisites section (must document the second-account requirement + APPROVER_TOKEN provisioning)"
    - "Phase 5 PORT-02 pre-flight (upstream needs an equivalent second-reviewer mechanism if running DOG-07 fresh; carry forward the option-a recommendation)"
    - "Phase 5 porting-prep — keep dog_07.py in the upstream-bound copy (the RFC §5 defense-in-depth invariant is RFC-mandated, not fork-specific)"
tech-stack:
  added: []
  patterns:
    - "Dual-client driver shape — run_scenario(client, *, approver_client) accepts a SECOND identity client used only for pulls.create_review + pulls.dismiss_review; the primary client owns PR open, /merge, polling. Unit tests pass the same FakeGitHub instance for both (the fake records the approver login internally); live mode constructs two GitHubClient(token=...) instances backed by GITHUB_TOKEN and APPROVER_TOKEN respectively. This is the first driver in the suite to require a non-primary identity."
    - "Two-phase polling — activation observation (mq:active label) followed by post-dismissal eject observation (status comment with the verbatim reason). Each phase receives the same timeout budget (TIMEOUT_S=900s); worst-case wall time bounded at 2x TIMEOUT_S but typical runs complete activation in ~9 min and eject in ~6 min. Mirrors the dog_05 two-poll structure."
    - "FakeGitHub extension owns dismiss_review state — the _DogfoodFake._review_state map is keyed by (pr_number, review_id) and is mutated APPROVED → DISMISSED on dismiss_review; the FakePR.reviews list is mirrored so a future test that exercises a second pulls.list_reviews read also observes the dismissal. The shared FakeGitHub in tests/gh_fake.py is NOT modified — every dogfood extension lives local to its own test module per the 03-10/03-13 pattern."
    - "Eject-injection seam (inject_eject_after) keyed on review-state observation — the predicate checks whether ANY review_id for the PR is currently DISMISSED before synthesizing the eject comment; otherwise it keeps polling. This guards against a regression where the eject is posted before the dismissal (the test would silently pass an incorrect ordering)."
key-files:
  created:
    - ".github/merge-queue/src/rocm_mq/dogfood/dog_07.py"
    - ".github/merge-queue/tests/test_dogfood_dog_07.py"
  modified: []
decisions:
  - "PRE-CONFIRM Task 1 resolution = option-a (second collaborator account + PAT). Recorded approver login placeholder: samuel-reeder-bot (the RESEARCH.md Area #17 recommendation; operator may substitute the actual provisioned login when running live). Rationale: option-a is the only path that (a) honors CONTEXT.md D-01 (fully scripted, no manual-step), (b) preserves RFC §5's approval-revocation property (option-c — relaxing branch protection to permit self-approval — would invalidate the very invariant DOG-07 is designed to demonstrate), and (c) mirrors the upstream model (Phase 5 PORT-02 pre-flight will need an equivalent reviewer account, so practicing on the fork lowers porting risk). Option-b (manual-step driver) is documented as the accepted fallback ONLY if option-a's PAT-provisioning is operationally blocked; option-c is explicitly rejected. The driver enforces option-a in main() via the APPROVER_TOKEN env-var gate (exit 2 with explanatory stderr if absent)."
  - "EXPECTED.reason is the verbatim RFC §6 literal 'approval revoked' (NOT a substring derived at runtime). Rationale: this string is RFC-defined; the production code emits it from a single literal in decision.py / executor.py, and a drift in either the driver or the production code must surface as a passed=False signal rather than be papered over by a substring match. The predicate compares the full literal; failure_mode_when_reason_mismatches test guards against a regression that loosens this to a substring."
  - "Activation is observed via the mq:active label (rest.pulls.get → labels), mirroring the dog_05 pattern. The processor's actual eject decision keys on the at-enqueue ≥1-approving-review gate failing on a subsequent cycle — the driver does not need to inspect that to verify the documented eject."
  - "Dual-client signature (approver_client kwarg) is REQUIRED rather than auto-derived from the primary client. Rationale: in live mode the two identities require distinct tokens (RFC §5 + branch protection forbid self-approval); making the second client a required kwarg surfaces the requirement at the call-site rather than hiding it inside the driver's main(). The unit tests pass the same FakeGitHub for both args because the fake tracks the approver login internally; live mode constructs two real GitHubClient instances. A targeted alternative — keying the second identity off an env var inside run_scenario itself — was rejected because it would couple run_scenario to environment state and complicate unit testing."
  - "FakeGitHub extension does NOT modify the shared tests/gh_fake.py. Rationale: the create_review / dismiss_review surfaces are dog_07-specific (no other dogfood driver needs them) and adding them to the shared fake would expand the blast radius of plan 03-15 to every dogfood test. The local _DogfoodFake class in test_dogfood_dog_07.py owns the extension; if a future scenario (e.g., a hypothetical DOG-09 review-state property) needs the same surface, the helpers can be promoted to gh_fake.py at that time without breaking dog_07's contract."
  - "Task 3 (checkpoint:human-verify — live fork run with APPROVER_TOKEN) is DEFERRED per the executor's objective ('Do NOT live-run. Unit tests only'). Mirrors plans 03-09 / 03-11 / 03-12 / 03-13 / 03-14's identical deferral pattern. The structural correctness of the driver is established by the 14-test unit suite + the plan's automated <verify> block (module constants); the operator's live fork run belongs to the post-deployment runbook and will produce an addendum to this SUMMARY once the second collaborator account is provisioned and APPROVER_TOKEN is available in the operator's environment."
metrics:
  duration: "~25 minutes (context-load + RED + GREEN + ruff/mypy gates + SUMMARY)"
  completed: 2026-05-20
---

# Phase 3 Plan 15: dog_07.py — Approval-Revoked Eject Driver Summary

Authored `.github/merge-queue/src/rocm_mq/dogfood/dog_07.py`, the DOG-07 dogfood driver that exercises the RFC §5 defense-in-depth property: the required-review gate must apply throughout the queue lifecycle, not just at enqueue time. A review dismissed AFTER activation MUST invalidate the activation and trigger an eject with the documented reason `"approval revoked"`. The driver creates a PR at an opted-in path, posts an APPROVE review from a second identity (the approver_client), posts `/merge` from the primary identity, polls until the `mq:active` label appears (activation succeeded), then dismisses the review via the approver_client, then polls until the processor's next cycle ejects with the verbatim RFC §6 reason. Two-mode driver mirroring plans 03-11..03-14: `run_scenario(client, *, approver_client, ...)` for unit-tested orchestration via a `_DogfoodFake` FakeGitHub extension; `main()` for `python -m rocm_mq.dogfood.dog_07 --owner ... --repo ...` against the live fork (operator-initiated AFTER the second collaborator account is provisioned; not exercised in CI per the executor's objective). Per-run JSON emission to `.planning/phases/03-handler-processor-on-fork/dogfood-runs/` honors the D-04 schema verbatim.

## What Shipped

- **`.github/merge-queue/src/rocm_mq/dogfood/dog_07.py`** (new, ~280 lines after ruff format) — module docstring referencing RFC §5 + §6 / CONTEXT.md D-04 / plan 03-15 Task 1 option-a outcome; constants `SCENARIO_ID="dog_07"`, `EXPECTED={"action": "Eject", "reason": "approval revoked"}` (verbatim RFC §6), `TIMEOUT_S=900` (15 min, RESEARCH.md Area #10), `_SEED_PATH_PREFIX="projects/hipdnn/dogfood-approval-revoked"` (opted-in path), `_APPROVER_TOKEN_ENV="APPROVER_TOKEN"`; helpers `_build_active_label_predicate()` (polls `rest.pulls.get` → inspects labels for `mq:active`, captures the head SHA at activation), `_build_eject_predicate()` (polls `rest.issues.list_comments` for a comment carrying both the `rocm-mq-status` marker and the verbatim `"approval revoked"` literal); `run_scenario(client, *, approver_client, ...)` orchestrates create-PR → APPROVE-via-approver → /merge → poll-activation → dismiss-via-approver → poll-eject → emit JSON; `main()` is the live-fork CLI entrypoint (lazy githubkit import, dual-token gating on GITHUB_TOKEN AND APPROVER_TOKEN, return-code 0/1/2 contract). Timeline records the 8-event subset called for by plan 03-15 Task 2 `<action>` block: `pr_opened`, `approval_review_submitted`, `merge_command_posted`, `mq_queued_label_applied`, `activation_began`, `mq_active_label_applied`, `approval_review_dismissed`, `ejected`. Ordering invariant tested explicitly: `approval_review_submitted` < `merge_command_posted` < `mq_active_label_applied` < `approval_review_dismissed` < `ejected`.

- **`.github/merge-queue/tests/test_dogfood_dog_07.py`** (new, ~510 lines after ruff format, 14 tests) — module constants (SCENARIO_ID, EXPECTED-verbatim-literal, TIMEOUT_S=900, main+run_scenario callability); happy-path orchestration (approval → /merge → activation → dismissal → eject with verbatim reason → passed=True, expected_outcome matches EXPECTED exactly, JSON emitted with passed=True); D-04 schema completeness (all 12 fields present in the emitted JSON); approval-via-approver-client invariant (exactly one review on the PR at scenario end, by `_APPROVER_LOGIN`, in state DISMISSED); dismissal-after-activation invariant (the `_review_state` map records DISMISSED only after the dismiss call runs, which is gated on the activation observation); failure mode 1 (eject with a different reason like `"merge conflict with develop"` → TimeoutError, no false-positive); failure mode 2 (activation never happens — driver times out at the activation poll and the dismissal step never fires, every recorded review remains APPROVED); timeline vocabulary (`approval_review_submitted` + `approval_review_dismissed` + `ejected` events present, with the documented ordering invariant); APPROVER_TOKEN env-var gates (missing GITHUB_TOKEN → exit 2; missing APPROVER_TOKEN → exit 2 with explanatory stderr).

## Second-account Mechanism (PRE-CONFIRM Task 1 outcome)

**Selected: option-a — second collaborator account with PAT.**

| Field | Value |
|-------|-------|
| Mechanism | Second collaborator account on `SamuelReeder/rocm-libraries`, added with write access; PAT scoped narrowly to `pull_requests:write` |
| Approver login (placeholder) | `samuel-reeder-bot` (operator may substitute the actually-provisioned login; the driver does not hard-code it — it reads the login from the create_review response and records it in the timeline + notes) |
| Env var | `APPROVER_TOKEN` (read by `main()`; absent → exit 2) |
| Token rotation | Operator-managed; documented in Phase 5 PORT-02 pre-flight (T-03-15-01 mitigation per plan 03-15 threat register) |

**Why not the alternatives:**
- **option-b (manual-step driver)** would have the driver `input()`-prompt the operator to click Approve / Dismiss in the GitHub UI. Violates CONTEXT.md D-01 (fully scripted, no operator-paste workflow). Accepted only as a fallback if option-a's PAT-provisioning is operationally blocked; not exercised today.
- **option-c (relax fork branch protection to allow self-approval)** would let the PR author approve their own PR. Invalidates the very RFC §5 invariant DOG-07 is designed to demonstrate — the same identity creating the PR could approve it, and "approval revoked" would no longer be a meaningful scenario. Explicitly rejected.

**Operator action required before live-run:**
1. Add a second account (e.g., `samuel-reeder-bot`) as a write-access collaborator on the fork (Settings → Collaborators).
2. Generate a fine-grained PAT for that account scoped to `pull_requests:write` on `SamuelReeder/rocm-libraries`.
3. Export both tokens before invoking the driver: `export GITHUB_TOKEN=...` (primary, App-equivalent) and `export APPROVER_TOKEN=...` (second-identity PAT).
4. Run: `cd .github/merge-queue && .venv/bin/python -m rocm_mq.dogfood.dog_07 --owner SamuelReeder --repo rocm-libraries`.

This procedure is also surfaced for plan 03-16's `DOGFOOD-RESULTS.md` prerequisites section and Phase 5 PORT-02's pre-flight checklist (the upstream repo will need an equivalent reviewer account if DOG-07 is replayed fresh).

## Commits

| Hash         | Kind | Subject                                                                                  |
| ------------ | ---- | ---------------------------------------------------------------------------------------- |
| `76a68421966` | test | test(03-15): add failing tests for dog_07 approval-revoked driver (RED gate)            |
| `202cfeedf41` | feat | feat(03-15): implement dog_07 approval-revoked eject driver (GREEN gate)                |

## Verification

Plan's `<verify><automated>` block ran clean against the GREEN commit:

```
$ cd .github/merge-queue && .venv/bin/python -c "from rocm_mq.dogfood import dog_07; \
    assert dog_07.SCENARIO_ID == 'dog_07'; \
    assert dog_07.EXPECTED['reason'] == 'approval revoked'; \
    assert dog_07.TIMEOUT_S == 900"
plan verify: OK
```

Unit suite for this plan (14 tests, all green):

```
$ .venv/bin/python -m pytest tests/test_dogfood_dog_07.py -q
.............. [100%]
14 passed in 0.35s
```

Full project regression (511 tests, all green — no existing test regressed under the new driver + test addition):

```
$ .venv/bin/python -m pytest tests/ -q
511 passed in 49.58s
8 snapshots passed.
```

Static checks (CLAUDE.md mandates):

```
$ .venv/bin/ruff check src/rocm_mq/dogfood/dog_07.py tests/test_dogfood_dog_07.py
All checks passed!
$ .venv/bin/ruff format --check src/rocm_mq/dogfood/dog_07.py tests/test_dogfood_dog_07.py
(both files already formatted after the ruff format pass folded into the GREEN commit)
$ .venv/bin/mypy --strict src/rocm_mq/dogfood/dog_07.py
Success: no issues found in 1 source file
```

## Deviations from Plan

None — plan executed exactly as written, with two pre-declared deferrals:

1. **Task 1 (checkpoint:decision)** — surfaced + resolved inline via option-a per the executor's objective ("PRE-CONFIRM resolution documented in SUMMARY"). The orchestrator's spawn prompt explicitly instructed the executor to "surface it to the orchestrator with options + recommendation"; the resolution is recorded in the **Second-account Mechanism** section above and is enforced at runtime by the `main()` APPROVER_TOKEN gate.
2. **Task 3 (checkpoint:human-verify — live fork run)** — DEFERRED per the executor's objective ("Do NOT live-run. Unit tests only."). Mirrors the identical deferral on plans 03-09 / 03-11 / 03-12 / 03-13 / 03-14. A live-fork addendum will append to this SUMMARY once the second collaborator account is provisioned and APPROVER_TOKEN is available.

## Threat Flags

None — the new driver does not introduce surface beyond what the plan's `<threat_model>` already enumerates (Spoofing T-03-15-01, Information Disclosure T-03-15-02, EoP T-03-15-03, Repudiation T-03-15-04). The unit tests do not touch the live API; the live-fork CLI path is documented but deferred.

## Self-Check: PASSED

- `.github/merge-queue/src/rocm_mq/dogfood/dog_07.py` — FOUND
- `.github/merge-queue/tests/test_dogfood_dog_07.py` — FOUND
- Commit `76a68421966` (RED) — FOUND in git log
- Commit `202cfeedf41` (GREEN) — FOUND in git log
- Plan automated verify — PASSED (constants + import)
- Unit suite (14 tests) — PASSED
- Full regression (511 tests) — PASSED
- ruff check — PASSED
- mypy --strict on dog_07.py — PASSED
