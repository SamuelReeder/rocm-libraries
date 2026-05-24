# Roadmap: Federated Merge Queue Reference Implementation

**Created:** 2026-05-14
**Granularity:** Coarse (5 phases)
**Execution:** Sequential (one plan at a time; intra-phase wave parallelization available)
**Coverage:** 51/51 v1 requirements mapped

## Project Reference

**Core Value:** Every algorithmic invariant in RFC 0001 §6 holds on a real GitHub repo — head-of-all-queues, App-created activation status binding, idempotent re-enqueue, FIFO order — demonstrated against a fork running real CI before the implementation is offered to upstream.

**Design Contract:** RFC 0001 (`docs/rfcs/0001_MergeQueue.md`) is locked. Tweaks surfaced during implementation go into the RFC tweaks log (PORT-03), not into silent code-only deviations.

**Scope Boundary:** Fork-only. Project stops at "PR-ready" on `SamuelReeder/rocm-libraries`. No upstream merge.

## Phases

- [x] **Phase 1: Pure Decision Layer** - Frozen-dataclass state model + pure `decide_cycle` + property-tested §6 invariants on synthetic snapshots
- [x] **Phase 2: I/O Layer + Executor** - REST client, snapshot builder, action executor with activation state machine, in-memory fake, end-to-end loop on canned data
- [x] **Phase 3: Handler + Processor on Fork** - `mq-handler.yml`, `mq-processor.yml`, App registration on fork, full §6 fork-dogfood scenarios pass against real CI
- [ ] **Phase 4a: Audit + Validator** - `pull_request_target` audit + `PATH_TO_QUEUES` validator + self-bootstrap protection; full §4.3.1 tamper matrix demonstrably ejects on the fork. **RFC Rollout Stage 1 (upstream commit)** requirement.
- [ ] **Phase 5: Porting Prep** - Operations runbook, pre-flight checklist, RFC tweaks log, README, squash-clean PR-ready branch against upstream `develop`. Ships **Stage 1** scope only (queue + audit + validator).
- [ ] **Phase 4b: Managed-Status** - `merge-queue/managed` poster workflow + processor flip-to-success hook. **RFC Rollout Stage 2** follow-up (non-blocking initially, promoted to required in Stage 3). Independent of Phase 5; can ship as a separate upstream PR after the queue beds in.

## Phase Details

### Phase 1: Pure Decision Layer
**Goal**: The §6 algorithm is correct in isolation — every invariant property-tested against synthetic queue snapshots without any GitHub I/O.
**Depends on**: Nothing (foundation)
**Requirements**: PURE-01, PURE-02, PURE-03, PURE-04, PURE-05, PURE-06, PURE-07, PURE-08, PURE-09
**Success Criteria** (what must be TRUE):
  1. `pytest` runs the full Hypothesis suite to completion and passes every RFC §6 invariant — head-of-all-queues, FIFO order, idempotent re-enqueue, no squash without an App-created `merge-queue/active` status, App-creator filter unspoofable — on synthetic `QueueSnapshot` fixtures.
  2. The RFC §4.2 worked example (5 PRs across 4 time-steps; A merges, then B/C in parallel, then D, then E) replays as a regression test producing the exact action sequence the RFC narrates.
  3. A CI lint job fails the build if any module under the pure-layer set (`state`, `decision`, `pathmap`, `comment`, `summary`, helpers) imports `requests`, `httpx`, `githubkit`, `os.environ`-readers, or any I/O-shaped symbol.
  4. `parse_gh_timestamp` rejects every naive datetime fixture and `is_app_identity` returns `False` for every fixture except the canonical App identity (slug + numeric ID + `type == "Bot"` all matching).
**Plans**: 5 plans
Plans:
- [x] 01-01-PLAN.md — Package scaffolding + state.py + helpers (PURE-01, PURE-05, PURE-06)
- [x] 01-02-PLAN.md — pathmap.py + decision.py (derive_pr, derive_snapshot, decide_cycle) (PURE-02, PURE-03)
- [x] 01-03-PLAN.md — comment.py + summary.py renderers with syrupy snapshots (PURE-04)
- [x] 01-04-PLAN.md — Hypothesis property tests for §6 invariants (PURE-07)
- [x] 01-05-PLAN.md — RFC §4.2 worked-example regression + PURE-09 AST walker lint (PURE-08, PURE-09)

### Phase 2: I/O Layer + Executor
**Goal**: The full read → decide → execute loop runs end-to-end against an in-memory GitHub fake, with executor idempotency and activation-merge race handling validated against canned snapshots.
**Depends on**: Phase 1
**Requirements**: IO-01, IO-02, IO-03, IO-04, IO-05, IO-06, IO-07, DOG-01
**Success Criteria** (what must be TRUE):
  1. `python -m rocm_mq process-cycle --fake` completes a synthetic cycle against `gh_fake.py` from canned snapshots, dispatching the action list from `decide_cycle` without making any real GitHub API call.
  2. Per-handler idempotency tests pass: re-applying every executor action (re-merge develop, re-post `(SHA, context)` status, re-flip labels) on a second invocation produces the same end state with no duplicate side effects.
  3. Post-squash verification reads back the merged commit on `develop` and asserts `commit.parents[0]` matches the pre-merge tip plus tree-diff sanity (defends Pitfall 8 / GitHub Apr-2026 silent-corruption pattern); failure aborts the cycle and surfaces a structured alert.
  4. Contract tests pin `gh_fake.py` to real-GitHub semantics for `(SHA, context)` status overwrite, label-vs-timeline consistency lag, and App-vs-workflow creator distinctions; the same test suite passes against both fake and real client at the protocol boundary.
  5. Full per-commit Python suite (PURE + IO contract + executor + Hypothesis) runs green in CI on every commit to the fork branch.
**Plans**: 5 plans (4 original + 1 gap-closure)
Plans:
- [x] 02-01-PLAN.md — gh.py thin client + AppIdentity resolution + PURE-09 I/O lint + Phase 1 ruff cleanup (IO-01)
- [x] 02-02-PLAN.md — snapshot.py builder + gh_fake.py in-memory fake + contract tests (IO-02, IO-06)
- [x] 02-03-PLAN.md — executor.py dispatch + activation state machine + idempotency + post-squash verification (IO-03, IO-04, IO-05)
- [x] 02-04-PLAN.md — cmd_process.py CLI entrypoint + end-to-end test + GHA CI workflow (IO-07, DOG-01)
- [x] 02-05-PLAN.md — gap closure for SC#3 tree-diff sanity in _verify_squash (Phase B via repos.compare_commits)

### Phase 3: Handler + Processor on Fork
**Goal**: A real fork PR can be `/merge`d and squash-merged through the queue end-to-end, with all RFC §6 happy-path and edge-case scenarios demonstrated against the fork's real CI.
**Depends on**: Phase 2
**Requirements**: WF-01, WF-02, WF-03, WF-04, WF-05, WF-06, WF-07, WF-08, WF-09, WF-10, WF-11, WF-12, DOG-02, DOG-03, DOG-04, DOG-05, DOG-06, DOG-07, DOG-08
**Success Criteria** (what must be TRUE):
  1. The merge-queue GitHub App is registered on `SamuelReeder/rocm-libraries` with the RFC §4.9 minimal permission set, its private key stored in an `mq-secrets` Environment scoped to `develop`-only deployment branches, and `gh api repos/SamuelReeder/rocm-libraries --jq .default_branch` returns `"develop"` (verified by a startup pre-flight check in the workflow).
  2. `/merge` on a fork PR: passes the live `repos.getCollaboratorPermissionLevel` eligibility check (not `author_association`), runs the at-enqueue gates (≥1 approval, no failing required check on head SHA, fork maintainer-edits enabled, queue set non-empty), applies `mq:queued` + `mq:<queue>` labels via the App token, and posts the single status comment marked with `<!-- rocm-mq-status -->`. A second `/merge` on the same PR is acknowledged as a no-op.
  3. The 3-min `mq-processor.yml` cron + `workflow_dispatch:` fallback runs under a static literal `concurrency: mq-processor` group with `cancel-in-progress: false` and per-job `timeout-minutes`, executes the §4.6 algorithm (discover → derive → sort → ready → activate-or-evaluate, never both in same cycle), and writes a `$GITHUB_STEP_SUMMARY` containing per-queue depth, active PRs, cycle outcomes, and wall-time.
  4. Every RFC §6 fork-dogfood scenario demonstrably ejects with the documented reason on the fork against real CI: merge conflict at activation (`"merge conflict with develop"`), CI red during evaluation (named failed check), simultaneous `/merge` (idempotent, no duplicate state), author push between activation and squash (`"activation invalid (branch updated or label tampered)"`), approval revoked between enqueue and squash (`"approval revoked"`), `/merge` on PR touching no opted-in path (handler-level rejection).
  5. The full RFC §4.2 5-PR worked example replays end-to-end on the fork against real CI: A merges first, B and C merge in parallel, D merges next, E merges last — observable via PR timeline and processor `$GITHUB_STEP_SUMMARY` runs.
  6. Processor `--dry-run` flag emits the would-be `Action` list to stdout / `$GITHUB_STEP_SUMMARY` without invoking any executor mutator.
**Plans**: 17 plans
Plans:
- [x] 03-00-PLAN.md — Carry-forward: re-run /gsd-verify-work 2 to clear stale 02-VERIFICATION.md (operational hygiene only)
- [x] 03-01-PLAN.md — Argparse subcommand refactor of cmd_process.py + handle/audit/preflight stubs (W-5 LOCKED debt repaid)
- [x] 03-02-PLAN.md — config.py with SELF_BOOTSTRAP_PATHS constant + load_from_develop loader + APP_SLUG/APP_ID env helpers (WF-08)
- [x] 03-03-PLAN.md — Hand-written path_to_queues.yml + live fork verification of paths/queues/required-check name
- [x] 03-04-PLAN.md — preflight.py module (default-branch + path_to_queues-loadable checks) + cmd_process.run_preflight wired (WF-10)
- [x] 03-05-PLAN.md — Operator: register rocm-mq-fork GitHub App on the fork + mq-secrets Environment with develop-only deployment-branch rule (WF-09)
- [x] 03-06-PLAN.md — cmd_handle.py: /merge + /dequeue parser, live perm check, at-enqueue gates, self-bootstrap rejection, label apply, status comment upsert, eyes-reaction (WF-01, WF-02, WF-03, WF-11, WF-12)
- [x] 03-07-PLAN.md — mq-handler.yml workflow: trigger surface + per-job permissions + audit-stub job (WF-01, WF-08, WF-09, WF-10)
- [x] 03-08-PLAN.md — mq-processor.yml workflow: cron + dispatch + STATIC concurrency + cycle-summary.md artifact upload (WF-04, WF-05, WF-06, WF-07, WF-08, WF-09, WF-10)
- [x] 03-09-PLAN.md — mq-dogfood-canary.yml workflow: title-marker deterministic CI-fail signal for DOG-03
- [x] 03-10-PLAN.md — dogfood subpackage scaffolding: __init__.py + _base.py (DogfoodResult + create_dogfood_pr + post_command + poll_pr_state + emit_result + download_cycle_summary_artifact)
- [x] 03-11-PLAN.md — DOG-02 + DOG-04 + DOG-08 drivers (short-scenario group)
- [x] 03-12-PLAN.md — DOG-03 driver (CI failure via canary)
- [x] 03-13-PLAN.md — DOG-05 driver (author push after activation)
- [x] 03-14-PLAN.md — DOG-06 driver: 5-PR RFC §4.2 worked example (marquee + Phase 5 porting-prep reference)
- [x] 03-15-PLAN.md — DOG-07 driver: approval revoked (PRE-CONFIRM second-account approver mechanism)
- [x] 03-16-PLAN.md — Dogfood aggregator + DOGFOOD-RESULTS.md generation (Phase 3 verification artifact for DOG-02..DOG-08)
**UI hint**: yes

### Phase 4a: Audit + Validator
**Goal**: Every row of the RFC §4.3.1 tamper matrix demonstrably triggers the documented response on the fork; self-bootstrap protection rejects every adversarial path-intersection variant; `PATH_TO_QUEUES` config edits without graph closure fail the validator. **Stage 1 hard requirement** — must ship in the upstream commit PR because tamper detection is the only safety property protecting `mq:*` labels from triage-user manipulation.
**Depends on**: Phase 3
**Requirements**: AUDIT-01, AUDIT-02, AUDIT-03, AUDIT-04, VAL-01, VAL-02, VAL-03, VAL-04
**Success Criteria** (what must be TRUE):
  1. The audit job in `mq-handler.yml` subscribed to `pull_request_target: [labeled, unlabeled, opened, reopened, edited, converted_to_draft]` ejects on every row of the RFC §4.3.1 tamper matrix (labels-at-open, post-open `mq:*` add/remove, base-ref change, draft conversion, close-then-reopen with stale labels), with an eject comment that distinguishes labels-at-open from post-open tampering, names the offending actor, and instructs `/merge` to re-enqueue. Demonstrated end-to-end on the fork.
  2. A CI lint enforces zero `actions/checkout` in any workflow that runs `pull_request_target` (`mq-handler.yml` audit job today; `mq-managed-status.yml` once Phase 4b lands); the lint fails the build if any future PR adds one. This defends the highest-impact bug class in the design surface (the `pull_request_target` PR-head-checkout trap).
  3. `mq-config-validate.yml` rejects every adversarial `PATH_TO_QUEUES` edit: missing-closure (downstream queue added but upstream entry not updated), self-bootstrap path-intersection bypass attempts (symlinks under `.github/`, case-only renames, generated-file regeneration paths). Property tests pin the rejection set.
  4. `PATH_TO_QUEUES` is loaded via the GitHub Contents API at `ref=develop` for handler and processor (`load_from_develop`); `load_local` is reachable only from the validator workflow. Unit + integration tests confirm a PR cannot modify its own queue routing by editing the config in its own branch.
**Plans**: TBD

### Phase 4b: Managed-Status
**Goal**: `merge-queue/managed` enforcement check live on every PR head SHA so branch protection on upstream `develop` can require it once Stage 3 is reached. Stage 2 of the RFC §7 rollout — non-blocking initially, promoted to required (Stage 3) after the queue has bedded in.
**Depends on**: Phase 4a (shares the no-checkout `pull_request_target` safety pattern that 4a's CI lint enforces). Independent of Phase 5: can ship as a separate upstream PR after the queue is live, on the Stage 2 timeline of the operator's choosing.
**Requirements**: MGD-01, MGD-02, MGD-03
**Success Criteria** (what must be TRUE):
  1. `mq-managed-status.yml` posts `merge-queue/managed` on every PR's head SHA — `pending` for opted-in paths, `success` for non-opted-in (so required-check semantics treat missing as blocking) — and the processor flips opted-in PRs from `pending` to `success` immediately before squash. Verified against fork PRs touching opted-in paths and PRs touching non-opted-in paths.
  2. The Stage-3 cutover procedure (operator promotes `merge-queue/managed` to a required check on `develop`) is documented as a runbook step in Phase 5's ops runbook — including bypass-actor configuration for the queue's App identity and a rollback recipe if the check misfires post-promotion.
**Plans**: TBD

### Phase 5: Porting Prep
**Goal**: A future implementer (or the upstream reviewer) can land this implementation on `ROCm/rocm-libraries` cleanly using the artifacts produced — runbook for operations, pre-flight checklist for upstream-side actions, RFC tweaks log for design round-tripping, and a squash-clean branch ready to PR. **Ships Stage 1 scope** (queue + audit + validator); managed-status (Phase 4b) is a separate follow-up PR on the operator's Stage 2 timeline.
**Depends on**: Phase 4a
**Requirements**: PORT-01, PORT-02, PORT-03, PORT-04, PORT-05
**Success Criteria** (what must be TRUE):
  1. The PR-ready branch on `SamuelReeder/rocm-libraries` produces a squash-clean diff against upstream `ROCm/rocm-libraries` `develop` (single coherent commit-set, no merge commits, no fork-only artifacts) — verified by `git diff upstream/develop...HEAD` review and a clean squash-preview.
  2. Operations runbook covers App key rotation (with `paused` flag + multi-key overlap window per Pitfall 16), compromise response, manual eject procedure, on-call playbook, missed-cycle detection (Pitfall 5), eviction monitoring (Pitfall 6), and audit-trail reconstruction — every section actionable by an on-call engineer at 2 AM without external context.
  3. Pre-flight checklist enumerates every upstream-side action required before `ROCm/rocm-libraries` can adopt the queue: App install, `mq-secrets` Environment + secrets, branch-protection state changes, default-branch verification (Pitfall 7), `merge-queue/managed` audit (Pitfall 14), `.github/labeler.yml` `mq:*` collision check, branch-protection-as-code path-glob update.
  4. RFC tweaks log records every implementation-surfaced edit/clarification (anchored to RFC section + line) ready to fold back into `docs/rfcs/0001_MergeQueue.md`; if the log is empty, that's an explicit assertion that no design deviations were needed.
  5. `.github/merge-queue/README.md` orients a new contributor in <5 minutes — architecture diagram (read → decide → execute), how to run tests locally, how to dry-run the processor against canned snapshots, where each workflow lives.
**Plans**: TBD

## Progress Table

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Pure Decision Layer | 5/5 | Complete | 2026-05-18 |
| 2. I/O Layer + Executor | 5/5 | Complete | 2026-05-19 |
| 3. Handler + Processor on Fork | 0/17 | Planned | - |
| 4a. Audit + Validator | 0/? | Not started | - |
| 4b. Managed-Status | 0/? | Not started | - |
| 5. Porting Prep | 0/? | Not started | - |

## Dependencies

```
Phase 1 (Pure)
   │
   ▼
Phase 2 (I/O + Executor) ── depends on §6 invariants from P1
   │
   ▼
Phase 3 (Handler + Processor on Fork) ── first phase touching real GitHub
   │
   ▼
Phase 4 (Audit + Managed + Validator) ── shares P3 infrastructure; tamper matrix is a different validation
   │
   ▼
Phase 5 (Porting Prep) ── all implementation surfaced; documentation + packaging
```

## Phase Ordering Rationale

- **Pure layer first** — RFC §4.9 makes the decision function the load-bearing correctness boundary. Validating it on synthetic snapshots first means every later phase is "wire I/O to a known-correct core," not "debug algorithm + I/O simultaneously."
- **Fakes before fork** — Executor bugs (idempotency, activation state machine, post-squash verification) are cheapest to find against `gh_fake.py` without burning real GitHub API budget or polluting the fork's audit trail.
- **Happy path before tamper matrix** — Phase 3 establishes that the queue *works* against real CI on the §6 happy-path scenarios; Phase 4 deliberately tries to break it via every §4.3.1 row. Different validation matrices, sequenced by risk.
- **Porting prep last** — All artifacts (runbook, pre-flight checklist, RFC tweaks log, README) derive from the finished implementation; producing them earlier would invite stale documentation.

## Research Flags

Phases likely needing deeper research during planning:
- **Phase 2:** `gh_fake.py` contract design — which GitHub semantics to model (status `(SHA, context)` overwrite, label-vs-timeline consistency lag per Pitfall 4, App-vs-workflow creator distinctions); confirm `githubkit` covers `repos.getCollaboratorPermissionLevel` or fall back to raw `httpx`.
- **Phase 3:** Concrete `gh` CLI / GitHub UI sequence to register the App + create `mq-secrets` Environment + scope to `develop`-only refs; pre-writes most of the Phase 5 pre-flight checklist.
- **Phase 4:** Re-verify branch-protection-as-code state on fork *and* upstream at start of phase; finalize `SELF_BOOTSTRAP_PATHS` constant.

Standard patterns (skip dedicated research):
- **Phase 1:** Heavily researched in STACK.md and ARCHITECTURE.md; pure-layer Python with Hypothesis is well-trodden.
- **Phase 5:** Documentation derived from finished implementation; no research needed.

---
*Roadmap created: 2026-05-14*
*Phase 2 plans added: 2026-05-18*
*Phase 3 plans added: 2026-05-19*
