"""
tests/_state_machine_base.py — Shared RuleBasedStateMachine base for RFC §6 invariant tests.

This base class provides:
  - shadow_prs: dict[int, PRState]          — every PR currently in the synthetic snapshot
  - shadow_queue_order: dict[str, list[int]] — expected FIFO order of PR numbers per queue
  - shadow_pending_timeline: list[tuple[int, LabelEvent]] — events not yet visible to derive
  - shadow_activation_history_per_queue: dict[str, list[Activate]] — for FIFO invariant
  - last_cycle_actions: list[Action]         — actions from the most recent advance_cycle call
  - shadow_raw_prs: dict[int, RawPRState]   — raw PR state for re-derive after mutations

Shadow-state pattern (Pitfall 11): Bundle is write-only outside @rule bodies;
@invariant methods read only self.shadow_* attributes, NEVER self.prs (the Bundle).

simulate_timeline_lag rule (Pitfall 4): defers App-applied mq:queued events by
one cycle, exercising the timeline-eventual-consistency code path in derive_pr.

canonical_merge_queue_config() is imported from tests.conftest (Plan 03 single
owning home) — NOT from tests._strategies. Wave 4 parallelism with Plan 05 preserved.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis.stateful import Bundle, RuleBasedStateMachine, initialize, rule

from rocm_mq.decision import decide_cycle, derive_pr
from rocm_mq.state import (
    Action,
    Activate,
    CommitStatus,
    Eject,
    LabelEvent,
    PRState,
    RawPRState,
    Snapshot,
    Squash,
)
from tests._strategies import raw_pr_strategy

# Single owning home for canonical config (Plan 03 territory — do NOT import
# canonical_merge_queue_config from tests._strategies).
from tests.conftest import CANONICAL_APP, canonical_merge_queue_config


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> datetime:
    """Internal tz-aware datetime helper for state machine initialization."""
    return datetime(year, month, day, hour, minute, tzinfo=UTC)


class MergeQueueStateMachineBase(RuleBasedStateMachine):
    """Shared scaffolding for §6 invariant state machines.

    Subclasses MUST:
    1. Override at least one @invariant() method.
    2. Read only self.shadow_* attributes in @invariant methods (Pitfall 11).
    3. NEVER access self.prs (the Bundle) from an @invariant method.

    The per-invariant test files expose pytest entry points via:
        TestCase = <SubclassName>.TestCase
    """

    prs = Bundle("prs")

    # NOTE: Per-class Hypothesis settings are applied via @settings(...) decorator
    # on each subclass (not via class attribute). Subclasses define their own
    # max_examples and stateful_step_count.

    # ------------------------------------------------------------------
    # @initialize — sets up all shadow state
    # ------------------------------------------------------------------

    @initialize()
    def setup(self) -> None:
        """Initialize shadow state. Called exactly once before any rules run."""
        self.config = canonical_merge_queue_config()

        # Shadow PR state — keyed by PR number
        self.shadow_prs: dict[int, PRState] = {}
        # Raw PR state — keyed by PR number, for re-derive after mutations
        self.shadow_raw_prs: dict[int, RawPRState] = {}

        # Expected FIFO order — per queue, list of PR numbers in enqueued_at order
        self.shadow_queue_order: dict[str, list[int]] = {
            q: [] for q in self.config.all_queues
        }

        # Events deferred by simulate_timeline_lag (Pitfall 4)
        # Each entry is (pr_number, app_label_event_to_add)
        self.shadow_pending_timeline: list[tuple[int, LabelEvent]] = []

        # Activation history per queue — for FIFO invariant
        self.shadow_activation_history_per_queue: dict[str, list[Activate]] = {
            q: [] for q in self.config.all_queues
        }

        # Actions from the most recent advance_cycle call.
        # Cleared at the START of each advance_cycle (not at the end), so that
        # invariant checks between non-cycle rules see an empty list (no stale actions).
        self.last_cycle_actions: list[Action] = []

        # Pre-apply snapshot of shadow_queue_order (taken at start of advance_cycle
        # before _apply_actions_to_shadow modifies the queue order).
        # Used by head-of-all-queues invariant to compare against the queue order
        # BEFORE squashes removed PRs from it.
        self.last_cycle_pre_queue_order: dict[str, list[int]] = {}

        # Track which PR numbers were squashed AND had canonical activation at squash time.
        # Used by squash_implies_canonical_activation_in_raw invariant (the PR is removed
        # from shadow_raw_prs during _apply_actions_to_shadow, so we cache the verdict).
        self.shadow_squash_had_canonical_activation: dict[int, bool] = {}

        # Set of PR numbers that had mq:active BEFORE the most recent advance_cycle call.
        # Used by no_double_activate invariant (post-apply, shadow_prs is updated, so
        # this pre-cycle snapshot is needed to distinguish "just activated" from "double-activate").
        self.last_cycle_pre_active_prs: frozenset[int] = frozenset()

        # Monotonically increasing PR number counter
        self.next_pr_number: int = 1

        # Current cycle time — advanced 3 minutes per advance_cycle call
        self.now: datetime = _utc(2026, 1, 1, 12, 0)

    # ------------------------------------------------------------------
    # @rule: enqueue — adds a new PR to the shadow state
    # ------------------------------------------------------------------

    @rule(target=prs, raw_pr=raw_pr_strategy())
    def enqueue(self, raw_pr: RawPRState) -> RawPRState:
        """Add a new PR to the synthetic snapshot.

        Assigns a fresh PR number and adds the PR to shadow state.
        With ~50% probability, defers the App-applied mq:queued event to the
        shadow_pending_timeline (Pitfall 4 simulation: label visible but event
        not yet in timeline API response).

        Returns the raw_pr (with updated number) so the Bundle can reference it.
        """
        import random

        number = self.next_pr_number
        self.next_pr_number += 1

        # Compute the minimum valid enqueued_at: the new PR must have enqueued_at
        # >= the maximum enqueued_at of all PRs currently in any of its queues.
        # This maintains the FIFO invariant: a new enqueue can never jump to the
        # front of a queue that already has older PRs waiting.
        #
        # We use self.now as a safe floor (the real system applies labels at "now").
        # This ensures monotonically non-decreasing enqueued_at across enqueue calls.
        min_valid_at = self.now

        # Normalize App-applied label events to use the clamped timestamp
        normalized_events = tuple(
            LabelEvent(
                label_name=e.label_name,
                event=e.event,
                actor=e.actor,
                created_at=min_valid_at,  # always use self.now as the enqueue time
            )
            for e in raw_pr.mq_queued_label_events
        )

        # Rebuild raw_pr with the assigned number and normalized events
        raw_pr = RawPRState(
            number=number,
            head_sha=raw_pr.head_sha,
            labels=raw_pr.labels,
            head_statuses=raw_pr.head_statuses,
            mq_queued_label_events=normalized_events,
            required_check_results=raw_pr.required_check_results,
            changed_paths=raw_pr.changed_paths,
        )

        # Check if we have App-applied queued events to possibly defer
        app_queued_events = tuple(
            e
            for e in raw_pr.mq_queued_label_events
            if e.event == "labeled"
            and e.label_name == self.config.queued_label
            and e.actor.type == "Bot"
            and e.actor.user_id == CANONICAL_APP.bot_user_id
        )

        if app_queued_events and random.random() < 0.5:
            # Defer: store raw_pr WITHOUT the App event (simulates Pitfall 4 lag)
            deferred_event = app_queued_events[0]
            raw_without_event = RawPRState(
                number=number,
                head_sha=raw_pr.head_sha,
                labels=raw_pr.labels,
                head_statuses=raw_pr.head_statuses,
                mq_queued_label_events=(),  # timeline not visible yet
                required_check_results=raw_pr.required_check_results,
                changed_paths=raw_pr.changed_paths,
            )
            self.shadow_raw_prs[number] = raw_without_event
            self.shadow_pending_timeline.append((number, deferred_event))
            # Do not add to shadow_prs yet — derive will return DeferredPR
        else:
            # Immediate: store raw_pr with events visible
            self.shadow_raw_prs[number] = raw_pr
            result = derive_pr(raw_pr, self.config, self.now)
            if isinstance(result, PRState):
                self.shadow_prs[number] = result
                # Add to queue order for each queue
                for q in result.queues:
                    if q in self.shadow_queue_order and number not in self.shadow_queue_order[q]:
                        self.shadow_queue_order[q].append(number)
                        # Sort by enqueued_at to maintain FIFO order
                        self.shadow_queue_order[q].sort(
                            key=lambda n: self.shadow_prs[n].enqueued_at
                            if n in self.shadow_prs
                            else self.now
                        )

        return raw_pr  # Bundle entry is the raw_pr

    # ------------------------------------------------------------------
    # @rule: push_new_commit — simulates a new commit being pushed to a PR
    # ------------------------------------------------------------------

    @rule(pr=prs)
    def push_new_commit(self, pr: RawPRState) -> None:
        """Simulate a new commit pushed to a PR.

        Clears the merge-queue/active status from head_statuses (the new commit
        invalidates the prior activation), models RFC §4.5 branch-update invalidation.
        Updates the raw_pr in shadow state and re-derives the PRState.
        """
        number = pr.number
        if number not in self.shadow_raw_prs:
            return  # PR not in shadow state (was deferred), skip

        existing_raw = self.shadow_raw_prs[number]
        new_sha = existing_raw.head_sha[:38] + "ff"  # deterministic new sha

        # Clear activation statuses on the new commit
        new_statuses = tuple(
            s for s in existing_raw.head_statuses
            if s.context != self.config.activation_status_context
        )

        # Clear mq:active from labels (post-activation label, cleared by new commit)
        new_labels = existing_raw.labels - {self.config.active_label}

        updated_raw = RawPRState(
            number=number,
            head_sha=new_sha,
            labels=new_labels,
            head_statuses=new_statuses,
            mq_queued_label_events=existing_raw.mq_queued_label_events,
            required_check_results=existing_raw.required_check_results,
            changed_paths=existing_raw.changed_paths,
        )
        self.shadow_raw_prs[number] = updated_raw

        # Re-derive and update shadow_prs
        result = derive_pr(updated_raw, self.config, self.now)
        if isinstance(result, PRState):
            self.shadow_prs[number] = result
        elif number in self.shadow_prs:
            # Derivation failed — remove from shadow snapshot
            del self.shadow_prs[number]

    # ------------------------------------------------------------------
    # @rule: simulate_timeline_lag — promotes deferred timeline events
    # ------------------------------------------------------------------

    @rule()
    def simulate_timeline_lag(self) -> None:
        """Promote one deferred timeline event to visible state.

        This is the Pitfall 4 chokepoint: it exercises the code path where the
        mq:queued label is present (visible in the label list) but the App-applied
        timeline event is not yet visible via the GitHub timeline API.

        When we promote the event, derive_pr transitions from Case 2 (DeferredPR)
        to Case 1 (normal PRState), and the PR enters the decision algorithm.
        """
        if not self.shadow_pending_timeline:
            return  # Nothing to promote

        number, deferred_event = self.shadow_pending_timeline.pop(0)

        if number not in self.shadow_raw_prs:
            return  # PR was removed (ejected/squashed), skip

        existing_raw = self.shadow_raw_prs[number]

        # Add the deferred event back to the raw PR
        promoted_raw = RawPRState(
            number=number,
            head_sha=existing_raw.head_sha,
            labels=existing_raw.labels,
            head_statuses=existing_raw.head_statuses,
            mq_queued_label_events=(*existing_raw.mq_queued_label_events, deferred_event),
            required_check_results=existing_raw.required_check_results,
            changed_paths=existing_raw.changed_paths,
        )
        self.shadow_raw_prs[number] = promoted_raw

        result = derive_pr(promoted_raw, self.config, self.now)
        if isinstance(result, PRState):
            self.shadow_prs[number] = result
            for q in result.queues:
                if q in self.shadow_queue_order and number not in self.shadow_queue_order[q]:
                    self.shadow_queue_order[q].append(number)
                    self.shadow_queue_order[q].sort(
                        key=lambda n: self.shadow_prs[n].enqueued_at
                        if n in self.shadow_prs
                        else self.now
                    )

    # ------------------------------------------------------------------
    # @rule: advance_cycle — calls decide_cycle and applies actions
    # ------------------------------------------------------------------

    @rule()
    def advance_cycle(self) -> None:
        """Run one decide_cycle tick and apply its actions to shadow state.

        Builds a Snapshot from shadow_prs, calls decide_cycle, applies the
        resulting actions to shadow state, records actions in last_cycle_actions,
        and advances self.now by 3 minutes.

        Per-PR invariant preserved: no PR ever gets Activate+Squash in the same cycle.
        """
        # Clear last_cycle_actions at the START so that between-cycle invariant checks
        # (fired after enqueue/push_new_commit/simulate_timeline_lag rules) see [].
        self.last_cycle_actions = []

        snapshot = self._build_snapshot_from_shadow()

        # Capture pre-apply snapshots (for invariants that compare against pre-cycle state)
        self.last_cycle_pre_active_prs = frozenset(
            pr.number
            for pr in snapshot.prs
            if self.config.active_label in pr.labels
        )
        # Deep-copy queue order BEFORE apply (for head-of-all-queues invariant)
        self.last_cycle_pre_queue_order = {
            q: list(order) for q, order in self.shadow_queue_order.items()
        }

        # Clear per-cycle squash cache before applying actions
        self.shadow_squash_had_canonical_activation.clear()

        actions = decide_cycle(snapshot, self.config, self.now)
        self._apply_actions_to_shadow(actions)
        self.last_cycle_actions = actions
        self.now += timedelta(minutes=3)

    # ------------------------------------------------------------------
    # Helper methods (called by rules and subclass invariants)
    # ------------------------------------------------------------------

    def _build_snapshot_from_shadow(self) -> Snapshot:
        """Build a Snapshot from current shadow_prs."""
        return Snapshot(prs=tuple(self.shadow_prs.values()))

    def _apply_actions_to_shadow(self, actions: list[Action]) -> None:
        """Apply decide_cycle actions to shadow state.

        - Activate(pr): add mq:active label; remove mq:queued; record in history
        - Squash(pr): remove PR from all shadow dicts and queue orders
        - Eject(pr, _): same as Squash — PR leaves the queue
        - UpdateComment / Defer: no shadow state change
        """
        for action in actions:
            if isinstance(action, Activate):
                pr_num = action.pr.number
                if pr_num not in self.shadow_raw_prs:
                    continue

                existing_raw = self.shadow_raw_prs[pr_num]
                # Update labels: add mq:active, remove mq:queued (per RFC §4.3)
                new_labels = (
                    existing_raw.labels - {self.config.queued_label}
                ) | {self.config.active_label}

                # Add canonical activation status to head_statuses
                canonical_status = CommitStatus(
                    context=self.config.activation_status_context,
                    state="success",
                    creator=_CANONICAL_CREATOR_FOR_SHADOW,
                    created_at=self.now,
                )
                updated_raw = RawPRState(
                    number=pr_num,
                    head_sha=existing_raw.head_sha,
                    labels=new_labels,
                    head_statuses=(*existing_raw.head_statuses, canonical_status),
                    mq_queued_label_events=existing_raw.mq_queued_label_events,
                    required_check_results=existing_raw.required_check_results,
                    changed_paths=existing_raw.changed_paths,
                )
                self.shadow_raw_prs[pr_num] = updated_raw

                # Re-derive PRState with updated labels + activation status
                result = derive_pr(updated_raw, self.config, self.now)
                if isinstance(result, PRState):
                    self.shadow_prs[pr_num] = result

                # Record in activation history per queue
                for q in action.pr.queues:
                    if q in self.shadow_activation_history_per_queue:
                        self.shadow_activation_history_per_queue[q].append(action)

            elif isinstance(action, Squash):
                pr_num = action.pr.number
                # Cache canonical activation verdict BEFORE removing the PR from shadow.
                # The @invariant squash_implies_canonical_activation_in_raw reads this
                # cache because shadow_raw_prs will be empty after _remove_pr_from_shadow.
                had_canonical = self._raw_had_canonical_activation(pr_num)
                self.shadow_squash_had_canonical_activation[pr_num] = had_canonical
                self._remove_pr_from_shadow(pr_num)

            elif isinstance(action, Eject):
                pr_num = action.pr.number
                self._remove_pr_from_shadow(pr_num)

    def _remove_pr_from_shadow(self, pr_num: int) -> None:
        """Remove a PR from all shadow state (used for Squash and Eject)."""
        self.shadow_prs.pop(pr_num, None)
        self.shadow_raw_prs.pop(pr_num, None)
        for queue_list in self.shadow_queue_order.values():
            if pr_num in queue_list:
                queue_list.remove(pr_num)

    def _raw_had_canonical_activation(self, pr_number: int) -> bool:
        """Return True iff the raw_pr's head_statuses contained a canonical activation.

        Used by the stronger invariant in test_invariant_no_squash_without_app_activation.
        Checks the underlying raw state (not the derived PRState) to verify the
        activation status creator was the canonical App identity.

        For PRs that have already been squashed (and thus removed from shadow_raw_prs),
        uses the cached verdict stored in shadow_squash_had_canonical_activation
        by _apply_actions_to_shadow BEFORE the PR was removed.
        """
        # First check the cache (for PRs removed by Squash action this cycle)
        if pr_number in self.shadow_squash_had_canonical_activation:
            return self.shadow_squash_had_canonical_activation[pr_number]

        raw = self.shadow_raw_prs.get(pr_number)
        if raw is None:
            # PR was removed by some other means — cannot verify; return False
            return False
        return any(
            s.context == self.config.activation_status_context
            and s.creator.type == "Bot"
            and s.creator.app_slug == CANONICAL_APP.slug
            and s.creator.app_id == CANONICAL_APP.app_id
            for s in raw.head_statuses
        )


# ---------------------------------------------------------------------------
# Module-level canonical creator — used by _apply_actions_to_shadow
# to inject a real activation status when simulating Activate effects.
# ---------------------------------------------------------------------------

from rocm_mq.state import CommitStatusCreator  # noqa: E402

_CANONICAL_CREATOR_FOR_SHADOW = CommitStatusCreator(
    login="rocm-mq[bot]",
    type="Bot",
    app_slug=CANONICAL_APP.slug,
    app_id=CANONICAL_APP.app_id,
)
