"""
rocm_mq.decision — RFC §4.6 per-cycle algorithm: derive + decide.

Three public functions:
  - ``derive_pr(raw, config, now) -> PRState | DeferredPR``
  - ``derive_snapshot(raw, config, now) -> tuple[Snapshot, tuple[Defer, ...]]``
  - ``decide_cycle(snapshot, config, now) -> list[Action]``

Private helpers:
  - ``_evaluate_required_checks(results) -> Literal["all_passed", "any_failed", "pending"]``
  - ``_first_failed_check_name(results) -> str``

**Pure function contract (RFC §4.9 + PURE-09):**
  - No ``datetime.now()`` / ``datetime.utcnow()`` calls; ``now`` is always an arg.
  - No I/O: no ``os.environ``, no ``subprocess``, no ``requests``/``httpx``/``githubkit``.
  - No persistent state: every call reconstructs from the snapshot (RFC §4.6).

**Design decisions (CONTEXT.md):**
  - D-01: Pure-layer derive over a raw + derived dataclass split.
  - D-02: Creator filter (``is_app_identity``) applied in derive, not in I/O.
  - D-03: ``PRState`` decision-only; renderer fields in ``RenderContext``.
  - D-04: ``Snapshot`` flat tuple; ``now`` as separate arg.
  - Q1: ``derive_pr`` returns ``PRState | DeferredPR``.
  - Q4: ``Defer.pr`` is ``PRState | PartialPRState``.

**Pitfalls guarded:**
  - Pitfall 2 (impersonation): ``is_app_identity`` triple-check via ``_helpers``.
  - Pitfall 4 (timeline lag): three-case derive logic; Case 2 = label-without-event.
  - Pitfall 5 (exhaustiveness): ``case _: assert_never(verdict)`` in ``decide_cycle``.
  - Pitfall 7 (vacuous headship): ``if not pr.queues: return False`` in ``is_head_of_all``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, assert_never

from rocm_mq._helpers import is_app_identity, is_app_identity_actor
from rocm_mq.state import (
    Action,
    Activate,
    Defer,
    DeferredPR,
    Eject,
    LabelEvent,
    MergeQueueConfig,
    PartialPRState,
    PRState,
    RawPRState,
    RawSnapshot,
    RequiredCheckResult,
    Snapshot,
    Squash,
)

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _evaluate_required_checks(
    results: tuple[RequiredCheckResult, ...],
) -> Literal["all_passed", "any_failed", "pending"]:
    """Classify the overall verdict of required check results.

    Priority order:
    1. ``"any_failed"`` — at least one check is in ``{"failure", "error"}``.
    2. ``"pending"``    — at least one check is ``"pending"`` (and none failed).
    3. ``"all_passed"`` — all checks are in ``{"success", "neutral"}`` (or empty tuple).

    An empty tuple of results returns ``"all_passed"``: a PR with no required checks
    may be squash-merged (RFC §4.6 allows this; Plan 04 invariant tests sanity-check).
    """
    _FAILURE_STATES = frozenset({"failure", "error"})
    has_pending = False
    for r in results:
        if r.state in _FAILURE_STATES:
            return "any_failed"
        if r.state == "pending":
            has_pending = True
    if has_pending:
        return "pending"
    return "all_passed"


def _first_failed_check_name(results: tuple[RequiredCheckResult, ...]) -> str:
    """Return the ``name`` of the first failed (or errored) required check.

    Raises ``ValueError`` if no failed result is found — callers must only invoke
    this function after confirming ``_evaluate_required_checks`` returned
    ``"any_failed"``.
    """
    _FAILURE_STATES = frozenset({"failure", "error"})
    for r in results:
        if r.state in _FAILURE_STATES:
            return r.name
    raise ValueError("_first_failed_check_name called with no failed results")


# ---------------------------------------------------------------------------
# derive_pr — three-case enqueued_at logic (RESEARCH.md lines 703-767)
# ---------------------------------------------------------------------------


def derive_pr(
    raw: RawPRState,
    config: MergeQueueConfig,
    now: datetime,
) -> PRState | DeferredPR:
    """Pure derivation of raw API state → decision-layer PRState.

    Three cases (Pitfall 4 — timeline eventual consistency):

    **Case 3 — Tampered:** Non-App actor applied ``mq:queued`` AND no App-applied
    event exists. Returned as ``DeferredPR(reason="mq:queued applied by non-App actor")``.
    Phase 4 audit catches this too, but the pure layer surfaces it independently.

    **Case 2 — Timeline lag:** ``mq:queued`` label is present but no App-applied
    ``mq:queued`` label event has become visible yet (GitHub timeline eventual
    consistency). Returned as ``DeferredPR(reason="mq:queued label present but
    timeline event not yet visible")``. The next cycle will re-derive.

    **No evidence:** Neither label nor App-applied event. PR is not in the queue.
    Returned as ``DeferredPR(reason="no enqueue evidence")``. Should not normally
    appear in the raw snapshot (the query filter should exclude such PRs), but
    ``derive_pr`` is total — it never raises.

    **Case 1 — Normal:** At least one App-applied ``mq:queued`` event exists.
    ``enqueued_at = max(e.created_at for e in app_queued_events)`` — the most
    recent App-applied event wins, so a re-enqueue (remove+re-add the label) moves
    the PR to the back of the queue (correct FIFO semantics for re-enqueue).

    ``queues`` is derived from ``mq:<name>`` labels on the PR, EXCLUDING the two
    state labels (``mq:queued`` and ``mq:active``).

    ``is_validly_active`` requires BOTH the context filter (``status.context ==
    config.activation_status_context``) AND the creator filter
    (``is_app_identity(status.creator, config.app_identity)``). See D-02.

    Args:
        raw: Raw GitHub API state for one PR (constructed by Phase 2 ``snapshot.py``).
        config: Validated merge-queue configuration.
        now: Current cycle time (passed through; not used directly in derive logic
             but matches the function family signature for consistency with decide_cycle).

    Returns:
        ``PRState`` on normal derivation; ``DeferredPR`` on any of the three
        defer cases.
    """
    # -- Filter mq:queued label events by App identity (Pitfall 2 family) --
    app_queued_events: tuple[LabelEvent, ...] = tuple(
        e
        for e in raw.mq_queued_label_events
        if (
            e.event == "labeled"
            and e.label_name == config.queued_label
            and is_app_identity_actor(e.actor, config.app_identity)
        )
    )
    non_app_queued_events: tuple[LabelEvent, ...] = tuple(
        e
        for e in raw.mq_queued_label_events
        if (
            e.event == "labeled"
            and e.label_name == config.queued_label
            and not is_app_identity_actor(e.actor, config.app_identity)
        )
    )

    has_queued_label: bool = config.queued_label in raw.labels

    # Case 3: Tampered — non-App applied mq:queued, no App event
    if non_app_queued_events and not app_queued_events:
        return DeferredPR(
            number=raw.number,
            reason="mq:queued applied by non-App actor",
        )

    # Case 2: Timeline lag — label present but no App event visible yet
    if has_queued_label and not app_queued_events:
        return DeferredPR(
            number=raw.number,
            reason="mq:queued label present but timeline event not yet visible",
        )

    # No evidence at all
    if not app_queued_events:
        return DeferredPR(
            number=raw.number,
            reason="no enqueue evidence",
        )

    # Case 1: Normal — use most recent App-applied event (max = re-enqueue wins)
    enqueued_at: datetime = max(e.created_at for e in app_queued_events)

    # Derive queue membership from mq:<name> labels, excluding state labels
    queues: frozenset[str] = frozenset(
        lbl[len(config.label_prefix) :]
        for lbl in raw.labels
        if lbl.startswith(config.label_prefix)
        and lbl not in (config.queued_label, config.active_label)
    )

    # is_validly_active: context AND creator (both filters required — D-02, Pitfall 2)
    is_validly_active: bool = any(
        s.context == config.activation_status_context
        and is_app_identity(s.creator, config.app_identity)
        for s in raw.head_statuses
    )

    return PRState(
        number=raw.number,
        head_sha=raw.head_sha,
        labels=raw.labels,
        queues=queues,
        enqueued_at=enqueued_at,
        is_validly_active=is_validly_active,
        required_check_results=raw.required_check_results,
    )


# ---------------------------------------------------------------------------
# derive_snapshot — walks RawSnapshot, emits Defer for un-derivable PRs
# ---------------------------------------------------------------------------


def derive_snapshot(
    raw: RawSnapshot,
    config: MergeQueueConfig,
    now: datetime,
) -> tuple[Snapshot, tuple[Defer, ...]]:
    """Derive all PRs in a raw snapshot, collecting Defer actions for failures.

    For each ``RawPRState`` in ``raw.prs``:
    - If ``derive_pr`` returns ``PRState`` → add to ``Snapshot.prs``.
    - If ``derive_pr`` returns ``DeferredPR`` → synthesize a ``PartialPRState``
      from the raw fields and emit a ``Defer(pr=partial, reason=...)`` action.

    The returned ``tuple[Defer, ...]`` is pre-emitted: callers (``cmd_process.py``
    in Phase 2) prepend these to the action list from ``decide_cycle`` so that
    the full list covers all PRs (derived + deferred).

    Returns:
        ``(Snapshot, tuple[Defer, ...])``
    """
    pr_states: list[PRState] = []
    defers: list[Defer] = []

    for raw_pr in raw.prs:
        result = derive_pr(raw_pr, config, now)
        match result:
            case PRState() as pr:
                pr_states.append(pr)
            case DeferredPR(number=_, reason=r):
                # Q4 resolution: carry the partial raw state for the executor
                partial = PartialPRState(
                    number=raw_pr.number,
                    head_sha=raw_pr.head_sha,
                    labels=raw_pr.labels,
                )
                defers.append(Defer(pr=partial, reason=r))

    return Snapshot(prs=tuple(pr_states)), tuple(defers)


# ---------------------------------------------------------------------------
# decide_cycle — RFC §4.6 steps 3-5 (RESEARCH.md lines 622-687)
# ---------------------------------------------------------------------------


def decide_cycle(
    snapshot: Snapshot,
    config: MergeQueueConfig,
    now: datetime,
) -> list[Action]:
    """RFC §4.6 steps 3-5: bucket → sort → identify ready → state-machine dispatch.

    **Pure function.** No I/O. No ``datetime.now()``. No persistent state.

    Step 3: Build per-queue FIFO buckets from the flat snapshot.
      For each queue in ``config.all_queues``, collect all PRs that belong to
      that queue (from ``pr.queues``) and sort by ``enqueued_at`` ascending (FIFO).
      Per-cycle fresh sort — no stored head pointer (RFC §4.6 "no persistent state").

    Step 4: Identify ready PRs.
      ``is_head_of_all(pr)`` returns True iff ``pr`` is at position 0 in every
      queue in ``pr.queues``.  Returns False when ``pr.queues`` is empty (Pitfall 7
      guard — prevents ``all([])`` vacuous truth).

    Step 5: Per-PR state machine (each ready PR dispatched once per cycle):
      5a. No ``mq:active`` label → ``Activate(pr)``; ``continue`` (MANDATORY).
          Never emits any other action for the same PR this cycle.
          This is the "activation and evaluation never in same cycle per PR"
          invariant from RFC §4.6 closing bullet.
      5b. Has ``mq:active`` label:
          - NOT ``is_validly_active`` → ``Eject(pr, "activation invalid ...")``.
          - Valid + all checks passed → ``Squash(pr)``.
          - Valid + any check failed → ``Eject(pr, <first failed check name>)``.
          - Valid + all pending → no action (retry next cycle).

    **Q2 resolution (RESEARCH.md Open Question 2):**
    The "activation and evaluation never in same cycle" rule is PER-PR, not
    globally per-cycle. When PRs A (in queue "hipdnn") and B (in queue
    "miopen-provider") are both at the head of their respective disjoint queues,
    the cycle may correctly emit ``[Squash(A), Activate(B)]``: A is dispatched
    via 5b (already active, checks pass); B is dispatched via 5a (not yet active).
    Each PR gets exactly one action; neither gets Activate+Squash in the same cycle.

    Args:
        snapshot: Derived PR snapshot (output of ``derive_snapshot``).
        config: Validated merge-queue configuration.
        now: Current cycle time (not used in core logic; carried for future extensions
             and to match the function family signature).

    Returns:
        Ordered list of ``Action`` variants for the executor.  Order within
        disjoint queue sets is implementation-defined (RFC §4.6 "activation order
        across disjoint queue sets is unspecified").
    """
    actions: list[Action] = []

    # Step 3: Build per-queue FIFO buckets
    members_by_queue: dict[str, list[PRState]] = {q: [] for q in config.all_queues}
    for pr in snapshot.prs:
        for q in pr.queues:
            if q in members_by_queue:
                members_by_queue[q].append(pr)
    for q in members_by_queue:
        members_by_queue[q].sort(key=lambda p: p.enqueued_at)  # FIFO

    # Step 4: Identify ready PRs
    def is_head_of_all(pr: PRState) -> bool:
        """Return True iff pr is at position 0 in every queue it belongs to.

        Returns False for empty queues (Pitfall 7: guards against all([]) == True).
        """
        if not pr.queues:
            return False  # Pitfall 7: empty queues is never head-of-all
        return all(
            members_by_queue[q] and members_by_queue[q][0].number == pr.number
            for q in pr.queues
        )

    ready = [pr for pr in snapshot.prs if is_head_of_all(pr)]

    # Step 5: Per-PR state machine dispatch
    for pr in ready:
        if config.active_label not in pr.labels:
            # 5a: Not yet active — emit Activate and CONTINUE.
            # The continue is mandatory: it prevents any 5b action for this PR
            # in the same cycle, enforcing "activation and evaluation never happen
            # in the same cycle per PR" (RFC §4.6 closing bullet, Pitfall 5).
            actions.append(Activate(pr=pr))
            continue  # MANDATORY: no 5b dispatch for this PR this cycle

        # 5b: Has mq:active label — evaluate activation validity and check results
        if not pr.is_validly_active:
            # Branch updated or label tampered after activation — eject
            actions.append(
                Eject(
                    pr=pr,
                    reason="activation invalid (branch updated or label tampered)",
                )
            )
            continue

        # Activation is valid — evaluate required CI checks
        verdict = _evaluate_required_checks(pr.required_check_results)
        match verdict:
            case "all_passed":
                actions.append(Squash(pr=pr))
            case "any_failed":
                failed_name = _first_failed_check_name(pr.required_check_results)
                actions.append(Eject(pr=pr, reason=failed_name))
            case "pending":
                pass  # Retry next cycle — no action emitted
            case _:
                assert_never(verdict)  # exhaustiveness guard (mypy --strict)

    return actions
