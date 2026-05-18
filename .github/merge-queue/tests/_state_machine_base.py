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

from hypothesis import HealthCheck, settings
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

    # Per-class Hypothesis settings — subclasses can override.
    # CI profile (500 examples) overrides this via conftest.py profile loading.
    settings = settings(
        stateful_step_count=30,
        max_examples=200,
        suppress_health_check=[HealthCheck.too_slow],
        deadline=None,
    )

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

        # Actions from the most recent advance_cycle call
        self.last_cycle_actions: list[Action] = []

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

        # Rebuild raw_pr with the assigned number
        raw_pr = RawPRState(
            number=number,
            head_sha=raw_pr.head_sha,
            labels=raw_pr.labels,
            head_statuses=raw_pr.head_statuses,
            mq_queued_label_events=raw_pr.mq_queued_label_events,
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
        snapshot = self._build_snapshot_from_shadow()
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

            elif isinstance(action, (Squash, Eject)):
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
        """
        raw = self.shadow_raw_prs.get(pr_number)
        if raw is None:
            # PR was already removed from shadow — cannot verify; return False
            # (conservative: treat as "no canonical activation found")
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
