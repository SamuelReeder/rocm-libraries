"""RFC §6 invariant: idempotent re-enqueue.

Two sub-invariants:
1. decide_cycle is a pure function: calling it twice on the same snapshot
   with the same arguments returns canonically equal action lists (no
   non-determinism).
2. No double-activate: a PR that already has mq:active in its labels does
   NOT receive a second Activate action in the same cycle (the mandatory
   `continue` in 5a prevents this per RFC §4.6).

Uses RuleBasedStateMachine + shadow-state pattern. @invariant methods read
only self.shadow_* — never self.prs (the Hypothesis Bundle) — because
Bundle reads inside @invariant are not safe under Hypothesis's shrinker.

The state machine class is named IdempotentReEnqueueMachine (no Test prefix)
so pytest does not try to collect it directly as a test class.
"""

from __future__ import annotations

from hypothesis import HealthCheck, settings
from hypothesis.stateful import invariant

from rocm_mq.decision import decide_cycle
from rocm_mq.state import Activate
from tests._state_machine_base import MergeQueueStateMachineBase


def _canonicalize(actions: list) -> list:
    """Sort actions to a canonical order that absorbs implementation-defined ordering
    across disjoint queue sets (RFC §4.6: "activation order across disjoint sets
    is unspecified"). The sort key is (class name, PR number)."""
    return sorted(
        actions,
        key=lambda a: (a.__class__.__name__, a.pr.number),
    )


@settings(
    stateful_step_count=30,
    max_examples=500,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
class IdempotentReEnqueueMachine(MergeQueueStateMachineBase):
    """State machine for the idempotent re-enqueue invariant.

    Two sub-invariants: decide_cycle purity + no double-activate.
    """

    @invariant()
    def decide_cycle_is_pure(self) -> None:
        """decide_cycle must return canonically equal action lists on repeated calls.

        Reads shadow_prs to build a snapshot — uses shadow state only.
        Calls decide_cycle twice and compares canonicalized results.
        """
        snapshot = self._build_snapshot_from_shadow()
        a1 = decide_cycle(snapshot, self.config, self.now)
        a2 = decide_cycle(snapshot, self.config, self.now)
        c1 = _canonicalize(a1)
        c2 = _canonicalize(a2)
        assert c1 == c2, (
            f"IDEMPOTENCY VIOLATED: decide_cycle returned different action lists "
            f"on two calls with identical inputs. "
            f"Call 1: {a1}, Call 2: {a2}"
        )

    @invariant()
    def no_double_activate(self) -> None:
        """No PR that already has mq:active in its labels receives another Activate.

        After a PR is Activated in a prior cycle, its shadow state is updated to
        have mq:active in labels. The next call to decide_cycle sees this label
        and dispatches via 5b (not 5a), so a second Activate is never emitted.

        Also checks that within the SAME cycle, no PR number appears twice in
        the Activate list (the per-PR state machine in decide_cycle ensures this
        via the mandatory `continue` in 5a).

        Reads shadow_prs and last_cycle_actions — never self.prs.
        """
        # Within this cycle: no PR number appears twice in Activate actions
        seen_in_this_cycle: set[int] = set()
        for action in self.last_cycle_actions:
            if isinstance(action, Activate):
                pr_num = action.pr.number
                assert pr_num not in seen_in_this_cycle, (
                    f"DOUBLE-ACTIVATE IN SAME CYCLE: PR #{pr_num} received Activate "
                    f"twice in the same cycle. Mandatory 'continue' in 5a may be broken."
                )
                seen_in_this_cycle.add(pr_num)

        # Cross-cycle: PRs that had mq:active BEFORE this cycle should NOT receive Activate.
        # (Uses last_cycle_pre_active_prs — the pre-apply snapshot — because shadow_prs
        # is updated by _apply_actions_to_shadow BEFORE the @invariant checks run.)
        for action in self.last_cycle_actions:
            if isinstance(action, Activate):
                pr_num = action.pr.number
                assert pr_num not in self.last_cycle_pre_active_prs, (
                    f"DOUBLE-ACTIVATE CROSS-CYCLE: PR #{pr_num} already had "
                    f"'{self.config.active_label}' in labels BEFORE this cycle "
                    f"but received a new Activate action. "
                    f"Pre-cycle active PRs: {self.last_cycle_pre_active_prs}"
                )


# Pytest discovery entry point
TestIdempotentReEnqueue_TestCase = IdempotentReEnqueueMachine.TestCase
