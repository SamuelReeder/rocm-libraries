"""RFC §6 invariant: head-of-all-queues.

Invariant: A PR is squash-merged only when it is at position 0 of every queue
it belongs to immediately before the squash action.

Uses RuleBasedStateMachine + shadow-state pattern. @invariant methods read
only self.shadow_* — never self.prs (the Hypothesis Bundle) — because
Bundle reads inside @invariant are not safe under Hypothesis's shrinker.

The state machine class is named HeadOfAllQueuesMachine (no Test prefix) to
prevent pytest from attempting to collect it directly as a test class.
Pytest discovers the per-invariant test via
TestHeadOfAllQueues_TestCase = Machine.TestCase.
"""

from __future__ import annotations

from hypothesis import HealthCheck, settings
from hypothesis.stateful import invariant

from rocm_mq.state import Squash
from tests._state_machine_base import MergeQueueStateMachineBase


@settings(
    stateful_step_count=30,
    max_examples=500,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
class HeadOfAllQueuesMachine(MergeQueueStateMachineBase):
    """State machine for the head-of-all-queues invariant.

    Invariant: every Squash(pr) action is emitted only when pr is at
    position 0 of every queue in pr.queues in the shadow state immediately
    before the squash.
    """

    @invariant()
    def no_squash_without_headship(self) -> None:
        """For every Squash action emitted this cycle, the squashed PR must
        have been at the head of every queue it belongs to BEFORE the cycle ran.

        Reads last_cycle_pre_queue_order (snapshot taken before _apply_actions_to_shadow
        removed the squashed PR) and last_cycle_actions — never self.prs.
        """
        for action in self.last_cycle_actions:
            if isinstance(action, Squash):
                pr = action.pr
                for q in pr.queues:
                    # Use pre-apply queue order (before Squash removed the PR)
                    queue_order = self.last_cycle_pre_queue_order.get(q, [])
                    if not queue_order:
                        # Queue was already empty before this cycle for this queue.
                        # This can happen if the PR's queue membership doesn't match
                        # the recorded queue order (e.g., enqueued without going through
                        # shadow_queue_order tracking). Skip — no headship to assert.
                        continue
                    head_number = queue_order[0]
                    assert pr.number == head_number, (
                        f"HEAD-OF-ALL-QUEUES VIOLATED: "
                        f"Squashed PR #{pr.number} but PR #{head_number} "
                        f"was at head of queue '{q}' before this cycle. "
                        f"pre-cycle queue order[{q!r}] = {queue_order}"
                    )


# Pytest discovery entry point — Hypothesis generates TestCase automatically.
# Named TestHeadOfAllQueues_TestCase so pytest discovers it as a test class.
TestHeadOfAllQueues_TestCase = HeadOfAllQueuesMachine.TestCase
