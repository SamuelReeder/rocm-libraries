---
gsd_state_version: 1.0
milestone: v1.0
milestone_name: milestone
status: ready_to_plan
last_updated: "2026-05-21T05:00:00.000Z"
progress:
  total_phases: 5
  completed_phases: 3
  total_plans: 27
  completed_plans: 27
  percent: 60
---

# Project State

**Last updated:** 2026-05-14 (after roadmap creation)

## Project Reference

**Project:** Federated Merge Queue — Reference Implementation (RFC 0001)
**Core Value:** Every algorithmic invariant in RFC 0001 §6 holds on a real GitHub repo — head-of-all-queues, App-created activation status binding, idempotent re-enqueue, FIFO order — demonstrated against a fork running real CI before the implementation is offered to upstream.
**Design Contract:** RFC 0001 (`docs/rfcs/0001_MergeQueue.md`) — locked.
**Scope Boundary:** Fork-only (`SamuelReeder/rocm-libraries`). Stops at "PR-ready". No upstream merge.
**Current Focus:** Phase 04 — audit-managed-validator

## Current Position

Phase: 03 (handler-processor-on-fork) — COMPLETE (verified_with_deferrals; DOG-07 deferred)
Plan: 17 of 17

- **Milestone:** v1 fork-dogfooded reference implementation
- **Phase:** 04 (audit-managed-validator) — Ready to plan
- **Plan:** Not started
- **Status:** Ready to plan
- **Deferred:** DOG-07 (approval revoked) — needs 2nd fork collaborator + PORT-02 revert; see claude memory `dog-07-deferred`.
- **Progress:** [██████████] 100%

## Performance Metrics

| Metric | Value |
|--------|-------|
| v1 requirements total | 51 |
| v1 requirements mapped | 51 |
| v1 requirements unmapped | 0 |
| Phases | 5 |
| Phases complete | 0 |
| Plans complete | 0 |
| Phase 02-i-o-layer-executor P05 | 7min | 3 tasks | 4 files |

## Accumulated Context

### Key Decisions (carried from PROJECT.md)

| Decision | Rationale |
|----------|-----------|
| RFC 0001 is locked design contract | Tweaks go to RFC tweaks log (PORT-03), not silent code-only deviations |
| Implementation under `.github/merge-queue/` | Single-directory port boundary to upstream |
| Test stack: pytest + Hypothesis | RFC §6 emphasizes invariants; property tests are natural fit |
| Stop scope at fork-dogfood + porting prep | Upstream Phase 1+ requires org-side decisions outside implementer's control |
| Scope queue to hipDNN ecosystem only | RFC §4.1 explicit v1 scope |
| SC#3 closed via _verify_squash Phase B (repos.compare_commits) | Pass = status='ahead' AND non-empty files; any other shape (identical / behind / diverged / files=[]) raises CorruptSquashError. No retry budget (both SHAs already committed). |

### Outstanding Decisions for Planning

- Exact directory path under `.github/` — recommended `.github/merge-queue/`; freeze in Phase 1 (becomes part of `SELF_BOOTSTRAP_PATHS`).
- `githubkit` coverage of `repos.getCollaboratorPermissionLevel` — verify in Phase 2; raw `httpx` fallback if absent.
- Hypothesis `RuleBasedStateMachine` decomposition — resolve in Phase 1 once decision-function signature is stable.
- Upstream branch-protection-as-code state at PR-readiness — research at start of Phase 4 (or Phase 5 pre-flight).

### Todos

(None yet — populated during planning/execution.)

### Blockers

(None.)

## Session Continuity

**Worktree branch:** `users/sareeder/merge-queue-rfc`
**Fork remote:** `fork` → `git@github.com:SamuelReeder/rocm-libraries.git`
**Upstream remote:** `origin` → `git@github.com:ROCm/rocm-libraries.git`
**Latest artifact:** `.planning/phases/02-i-o-layer-executor/02-05-SUMMARY.md` (SC#3 closure, 2026-05-19)
**Next action:** `/gsd-verify-phase 02` to flip SC#3 from FAILED to VERIFIED

---
*Initialized: 2026-05-14*
