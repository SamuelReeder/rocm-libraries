"""
rocm_mq.decision — RFC §4.6 per-cycle algorithm: derive + decide.

Three public functions:
  - ``derive_pr(raw, config, now) -> PRState | DeferredPR``
  - ``derive_snapshot(raw, config, now) -> tuple[Snapshot, tuple[Defer, ...]]``
  - ``decide_cycle(snapshot, config, now) -> list[Action]``

Required-CI-check evaluation is delegated to GitHub branch protection so the
queue's config and branch protection cannot drift out of sync (RFC §4.8):
``decide_cycle`` unconditionally emits ``Squash`` for any validly-active
head-of-queue PR, and ``executor._handle_squash`` translates a merge-API
405/422 into ``Eject`` (failing required check) or no-op (pending check —
retry next cycle).

**Pure function contract (RFC §4.9):**
  - No ``datetime.now()`` / ``datetime.utcnow()`` calls; ``now`` is always an arg.
  - No I/O: no ``os.environ``, no ``subprocess``, no ``requests``/``httpx``/``githubkit``.
  - No persistent state: every call reconstructs from the snapshot (RFC §4.6).
"""

from __future__ import annotations

from datetime import datetime

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
    Snapshot,
    Squash,
)

# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# derive_pr — three-case enqueued_at logic
# ---------------------------------------------------------------------------


def derive_pr(
    raw: RawPRState,
    config: MergeQueueConfig,
    now: datetime,
) -> PRState | DeferredPR:
    """Pure derivation of raw API state → decision-layer PRState.

    Three cases for handling GitHub timeline eventual consistency:

    **Case 3 — Tampered:** Non-App actor applied ``mq:queued`` AND no App-applied
    event exists. Returned as ``DeferredPR(reason="mq:queued applied by non-App actor")``.
    The audit job catches this too, but the pure layer surfaces it independently.

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
    state labels (``mq:queued`` and ``mq:active``). For already-labelled PRs,
    the snapshot adapter may intentionally leave ``raw.changed_paths`` empty:
    after enqueue, labels are the authoritative queue-membership source.

    ``is_validly_active`` requires BOTH the context filter (``status.context ==
    config.activation_status_context``) AND the creator filter
    (``is_app_identity(status.creator, config.app_identity)``) — RFC §4.3.1.

    Args:
        raw: Raw GitHub API state for one PR.
        config: Validated merge-queue configuration.
        now: Current cycle time (passed through; not used directly in derive logic
             but matches the function family signature for consistency with decide_cycle).

    Returns:
        ``PRState`` on normal derivation; ``DeferredPR`` on any of the three
        defer cases.
    """
    # -- Filter mq:queued label events by App identity (RFC §4.3.1) --
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

    # Derive queue membership from mq:<name> labels, excluding state labels.
    # Do not read raw.changed_paths here: build_snapshot may skip list_files
    # for labelled PRs because labels are authoritative after enqueue.
    queues: frozenset[str] = frozenset(
        lbl[len(config.label_prefix) :]
        for lbl in raw.labels
        if lbl.startswith(config.label_prefix)
        and lbl not in (config.queued_label, config.active_label)
    )

    # is_validly_active: context AND creator (both filters required — RFC §4.3.1)
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

    The returned ``tuple[Defer, ...]`` is pre-emitted: the processor prepends
    these to the action list from ``decide_cycle`` so the full list covers all
    PRs (derived + deferred).

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
                # Carry the partial raw state so the executor can still
                # reference the PR (number/head_sha/labels) when reporting
                # the Defer outcome, even though full derivation failed.
                partial = PartialPRState(
                    number=raw_pr.number,
                    head_sha=raw_pr.head_sha,
                    labels=raw_pr.labels,
                )
                defers.append(Defer(pr=partial, reason=r))

    return Snapshot(prs=tuple(pr_states)), tuple(defers)


# ---------------------------------------------------------------------------
# decide_cycle — RFC §4.6 steps 3-5
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
      queue in ``pr.queues``. Returns False when ``pr.queues`` is empty — guards
      against the vacuous-truth case where ``all([])`` would otherwise return True
      and squash a PR with no queue membership at all.

    Step 5: Per-PR state machine (each ready PR dispatched once per cycle):
      5a. No ``mq:active`` label → ``Activate(pr)``; ``continue`` (MANDATORY).
          Never emits any other action for the same PR this cycle.
          This is the "activation and evaluation never in same cycle per PR"
          invariant from RFC §4.6 closing bullet.
      5b. Has ``mq:active`` label:
          - NOT ``is_validly_active`` → ``Eject(pr, "activation invalid ...")``.
          - Valid → ``Squash(pr)`` unconditionally; the executor translates
            branch-protection rejections into Eject/no-op (RFC §4.8).

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

        Returns False for PRs with no queue membership — guards against
        ``all([])`` returning True (vacuous truth would let an unrouted PR
        squash-merge).
        """
        if not pr.queues:
            return False
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
            # in the same cycle per PR" (RFC §4.6 closing bullet).
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

        # Activation is valid — emit Squash unconditionally. Required-check
        # evaluation is delegated to branch protection (the single source of
        # truth) so the queue and branch protection cannot drift out of sync
        # (RFC §4.8). The executor's _handle_squash translates a 405/422 from
        # the merge API into either Eject (failing required check, named in
        # the error body) or no-op (pending required check — try next cycle).
        actions.append(Squash(pr=pr))

    return actions
