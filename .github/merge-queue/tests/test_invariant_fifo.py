"""
test_invariant_fifo.py — RFC §6 invariant 2: FIFO ordering.

Invariant: Within a single queue, the order in which PRs receive Activate
actions respects their enqueued_at order WHEN VISIBLE IN THE SAME CYCLE.

Two FIFO invariants:
1. Within-cycle: for each queue in last_cycle_actions, the Activate action's
   pr.enqueued_at is the minimum enqueued_at of all visible (shadow_prs) PRs
   in that queue — i.e., the head-of-queue PR has the earliest enqueued_at.
2. Within-cycle uniqueness: at most one PR per queue is Activated in a single
   cycle (only the head can be activated).

NOTE on timeline lag: The RFC's FIFO guarantee applies within a single cycle's
visible snapshot. A PR with an earlier enqueued_at may enter shadow_prs AFTER
an Activate was already issued for a later-timestamped PR (Pitfall 4 — timeline
lag). The cross-cycle activation history is therefore NOT required to be globally
monotone; only the within-cycle ordering is invariant-testable.

Uses RuleBasedStateMachine + shadow-state pattern (Pitfall 11).
@invariant reads only self.shadow_* — never self.prs (the Bundle).

The state machine class is named FifoMachine (no Test prefix) to prevent
pytest from attempting to collect it directly as a test class.
"""

from __future__ import annotations

from hypothesis import HealthCheck, settings
from hypothesis.stateful import invariant

from rocm_mq.state import Activate
from tests._state_machine_base import MergeQueueStateMachineBase


@settings(
    stateful_step_count=30,
    max_examples=500,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
class FifoMachine(MergeQueueStateMachineBase):
    """State machine for the FIFO ordering invariant.

    Checks that within a single cycle, activated PRs were at the head (earliest
    enqueued_at) of their respective queues at activation time.
    """

    @invariant()
    def fifo_within_cycle(self) -> None:
        """For each Activate(pr) in last_cycle_actions, pr must have been at the
        head of each of its queues (minimum enqueued_at among visible PRs) at the
        time decide_cycle was called.

        Uses last_cycle_pre_queue_order (pre-apply queue order) to verify that
        the activated PR was at position 0 in shadow_queue_order before the cycle.
        Also verifies that at most one PR per queue is Activated in a single cycle.

        Reads last_cycle_pre_queue_order, last_cycle_actions — never self.prs.
        """
        # Count Activates per queue to enforce at-most-one-per-queue
        activates_per_queue: dict[str, list[int]] = {}

        for action in self.last_cycle_actions:
            if isinstance(action, Activate):
                pr = action.pr
                # Check enqueued_at ordering: pr must have been at head of each queue
                for q in pr.queues:
                    pre_cycle_order = self.last_cycle_pre_queue_order.get(q, [])
                    if not pre_cycle_order:
                        continue  # Queue was empty before cycle, skip
                    head_number = pre_cycle_order[0]
                    assert pr.number == head_number, (
                        f"FIFO VIOLATED in queue '{q}': "
                        f"Activated PR #{pr.number} (enqueued_at={pr.enqueued_at}) "
                        f"but PR #{head_number} was at head of queue before the cycle. "
                        f"pre-cycle order: {pre_cycle_order}"
                    )
                    activates_per_queue.setdefault(q, []).append(pr.number)

        # Verify at most one Activate per queue
        for q, activated in activates_per_queue.items():
            assert len(activated) <= 1, (
                f"FIFO VIOLATED (multiple activates same queue): "
                f"Queue '{q}' had {len(activated)} Activate actions in one cycle: "
                f"{activated}"
            )

    @invariant()
    def fifo_queue_order_is_sorted(self) -> None:
        """The shadow_queue_order for each queue must be sorted by enqueued_at.

        This verifies that our shadow state correctly tracks the FIFO order that
        decide_cycle would compute (via its own fresh sort on each cycle).

        Reads shadow_queue_order and shadow_prs — never self.prs.
        """
        for q, order in self.shadow_queue_order.items():
            if len(order) < 2:
                continue
            timestamps = [
                self.shadow_prs[n].enqueued_at
                for n in order
                if n in self.shadow_prs
            ]
            if len(timestamps) < 2:
                continue
            assert timestamps == sorted(timestamps), (
                f"SHADOW QUEUE ORDER NOT SORTED in queue '{q}': "
                f"order={order}, timestamps={timestamps}"
            )


# Pytest discovery entry point
TestFifo_TestCase = FifoMachine.TestCase
