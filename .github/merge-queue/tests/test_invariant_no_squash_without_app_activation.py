"""RFC §6 invariant: no squash without App-created activation.

Two sub-invariants:
1. For every Squash(pr), pr.is_validly_active is True (derived PRState check).
2. Stronger: the underlying raw_pr had a merge-queue/active status from the
   canonical App identity (raw canonical activation check).

Uses RuleBasedStateMachine + shadow-state pattern. @invariant methods read
only self.shadow_* — never self.prs (the Hypothesis Bundle) — because
Bundle reads inside @invariant are not safe under Hypothesis's shrinker.

The state machine class is named NoSquashWithoutActivationMachine (no Test
prefix) so pytest does not try to collect it directly; discovery happens via
TestNoSquashWithoutAppActivation_TestCase below.
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
class NoSquashWithoutActivationMachine(MergeQueueStateMachineBase):
    """State machine for the no-squash-without-App-activation invariant."""

    @invariant()
    def squash_requires_validly_active(self) -> None:
        """For every Squash in last_cycle_actions, pr.is_validly_active must be True.

        is_validly_active=True means derive_pr found a merge-queue/active
        commit status with BOTH the right context AND the canonical App
        creator (the activation-status creator filter, RFC §4.3.1).

        Reads last_cycle_actions — never self.prs.
        """
        for action in self.last_cycle_actions:
            if isinstance(action, Squash):
                assert action.pr.is_validly_active, (
                    f"NO-SQUASH-WITHOUT-ACTIVATION VIOLATED: "
                    f"Squashed PR #{action.pr.number} but is_validly_active is False. "
                    f"PR labels: {action.pr.labels}"
                )

    @invariant()
    def squash_implies_canonical_activation_in_raw(self) -> None:
        """Stronger check: for every Squash, the raw PR must have had a
        merge-queue/active status from the canonical App identity.

        This catches cases where is_validly_active might be incorrectly True
        but the raw activation is actually forged. Uses _raw_had_canonical_activation
        helper which reads shadow_raw_prs (shadow state only — safe (shadow state only)).

        Note: During the @invariant check, the PR is still in shadow_raw_prs
        (removal happens after the invariant check completes via Hypothesis's
        state machine loop). So this check always has access to the raw state.
        """
        for action in self.last_cycle_actions:
            if isinstance(action, Squash):
                pr_num = action.pr.number
                assert self._raw_had_canonical_activation(pr_num), (
                    f"NO-SQUASH-WITHOUT-CANONICAL-ACTIVATION VIOLATED: "
                    f"Squashed PR #{pr_num} but raw state had no canonical-App "
                    f"merge-queue/active status. "
                    f"is_validly_active={action.pr.is_validly_active}"
                )


# Pytest discovery entry point
TestNoSquashWithoutAppActivation_TestCase = NoSquashWithoutActivationMachine.TestCase
