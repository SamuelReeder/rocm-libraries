"""
tests/test_decide_cycle_unit.py — Focused unit tests for decide_cycle's
state-machine dispatch table (RESEARCH.md lines 691-700).

One test per dispatch row + bonus tests for:
- 5a continue enforcement (Activate only, never Activate+Squash for same PR)
- Pitfall 7: empty-queues guard (is_head_of_all returns False for frozenset())
- Q2 resolution: disjoint-queue PRs can be [Squash(A), Activate(B)] in same cycle

The Q2 resolution test includes an inline comment referencing RESEARCH.md Open
Question 2 resolution.
"""

from __future__ import annotations

from datetime import UTC, datetime

from rocm_mq.decision import decide_cycle
from rocm_mq.state import (
    Activate,
    AppIdentity,
    Eject,
    MergeQueueConfig,
    PRState,
    RequiredCheckResult,
    Snapshot,
    Squash,
)

# ---------------------------------------------------------------------------
# Canonical test config
# ---------------------------------------------------------------------------

_APP = AppIdentity(slug="rocm-mq", app_id=12345, bot_user_id=99999)

_CONFIG = MergeQueueConfig(
    all_queues=(
        "hipdnn",
        "miopen-provider",
    ),
    path_to_queues=(),
    app_identity=_APP,
)

_NOW = datetime(2026, 1, 1, tzinfo=UTC)

# A timestamp earlier than _NOW for enqueued_at
_T0 = datetime(2025, 12, 31, tzinfo=UTC)
_T1 = datetime(2025, 12, 30, tzinfo=UTC)


def _make_pr_state(
    number: int,
    queues: frozenset[str],
    has_active_label: bool = False,
    is_validly_active: bool = False,
    required_check_results: tuple[RequiredCheckResult, ...] = (),
    enqueued_at: datetime | None = None,
) -> PRState:
    """Factory for PRState test instances."""
    labels: frozenset[str] = frozenset({"mq:queued"})
    if has_active_label:
        labels = labels | frozenset({"mq:active"})
    return PRState(
        number=number,
        head_sha=f"sha{number}",
        labels=labels,
        queues=queues,
        enqueued_at=enqueued_at or _T0,
        is_validly_active=is_validly_active,
        required_check_results=required_check_results,
    )


def _all_passed_checks() -> tuple[RequiredCheckResult, ...]:
    return (RequiredCheckResult(name="CI / build", state="success"),)


def _failed_checks() -> tuple[RequiredCheckResult, ...]:
    return (RequiredCheckResult(name="CI / build", state="failure"),)


def _pending_checks() -> tuple[RequiredCheckResult, ...]:
    return (RequiredCheckResult(name="CI / build", state="pending"),)


# ---------------------------------------------------------------------------
# Row 1: ready + no mq:active → Activate(pr)
# ---------------------------------------------------------------------------


def test_dispatch_row1__ready_no_active_emits_activate() -> None:
    """Row 1: ready PR without mq:active → [Activate(pr)]."""
    pr = _make_pr_state(1, frozenset({"hipdnn"}), has_active_label=False)
    snapshot = Snapshot(prs=(pr,))
    actions = decide_cycle(snapshot, _CONFIG, _NOW)
    assert actions == [Activate(pr=pr)]


# ---------------------------------------------------------------------------
# Row 2: ready + mq:active + NOT is_validly_active → Eject
# ---------------------------------------------------------------------------


def test_dispatch_row2__ready_active_not_valid_emits_eject() -> None:
    """Row 2: ready + mq:active + is_validly_active=False → [Eject(...)]."""
    pr = _make_pr_state(
        2,
        frozenset({"hipdnn"}),
        has_active_label=True,
        is_validly_active=False,
    )
    snapshot = Snapshot(prs=(pr,))
    actions = decide_cycle(snapshot, _CONFIG, _NOW)
    assert len(actions) == 1
    eject = actions[0]
    assert isinstance(eject, Eject)
    assert eject.pr.number == 2
    assert "activation invalid" in eject.reason


# ---------------------------------------------------------------------------
# Row 3: ready + active + valid + all checks pass → Squash
# ---------------------------------------------------------------------------


def test_dispatch_row3__ready_active_valid_checks_pass_emits_squash() -> None:
    """Row 3: ready + active + valid + all checks passed → [Squash(pr)]."""
    pr = _make_pr_state(
        3,
        frozenset({"hipdnn"}),
        has_active_label=True,
        is_validly_active=True,
        required_check_results=_all_passed_checks(),
    )
    snapshot = Snapshot(prs=(pr,))
    actions = decide_cycle(snapshot, _CONFIG, _NOW)
    assert actions == [Squash(pr=pr)]


# ---------------------------------------------------------------------------
# Row 4: ready + active + valid + one check failed → Eject(name of failed check)
# ---------------------------------------------------------------------------


def test_dispatch_row4__ready_active_valid_checks_failed_emits_eject_with_name() -> None:
    """Row 4: ready + active + valid + one check failed → [Eject(pr, name)]."""
    pr = _make_pr_state(
        4,
        frozenset({"hipdnn"}),
        has_active_label=True,
        is_validly_active=True,
        required_check_results=_failed_checks(),
    )
    snapshot = Snapshot(prs=(pr,))
    actions = decide_cycle(snapshot, _CONFIG, _NOW)
    assert len(actions) == 1
    eject = actions[0]
    assert isinstance(eject, Eject)
    assert eject.pr.number == 4
    assert "CI / build" in eject.reason  # name of the failed check


# ---------------------------------------------------------------------------
# Row 5: ready + active + valid + all pending → no action
# ---------------------------------------------------------------------------


def test_dispatch_row5__ready_active_valid_checks_pending_emits_nothing() -> None:
    """Row 5: ready + active + valid + all pending → []."""
    pr = _make_pr_state(
        5,
        frozenset({"hipdnn"}),
        has_active_label=True,
        is_validly_active=True,
        required_check_results=_pending_checks(),
    )
    snapshot = Snapshot(prs=(pr,))
    actions = decide_cycle(snapshot, _CONFIG, _NOW)
    assert actions == []


# ---------------------------------------------------------------------------
# Row 6: not head (older PR in same queue ahead) → not in action list
# ---------------------------------------------------------------------------


def test_dispatch_row6__not_head_emits_nothing() -> None:
    """Row 6: PR not at head of queue (blocked by older PR) → not in actions."""
    pr_older = _make_pr_state(
        6,
        frozenset({"hipdnn"}),
        has_active_label=False,
        enqueued_at=_T1,  # older — is head
    )
    pr_newer = _make_pr_state(
        7,
        frozenset({"hipdnn"}),
        has_active_label=False,
        enqueued_at=_T0,  # newer — is NOT head
    )
    snapshot = Snapshot(prs=(pr_newer, pr_older))
    actions = decide_cycle(snapshot, _CONFIG, _NOW)
    # Only the head PR (pr_older, #6) should be in actions
    assert len(actions) == 1
    assert isinstance(actions[0], Activate)
    assert actions[0].pr.number == 6


# ---------------------------------------------------------------------------
# Row 7 (Pitfall 7): PR with queues == frozenset() → not in action list
# ---------------------------------------------------------------------------


def test_dispatch_row7__empty_queues_guard() -> None:
    """Row 7 (Pitfall 7): PR with queues=frozenset() is never head-of-all."""
    pr = _make_pr_state(
        8,
        frozenset(),  # empty queues — vacuous all([]) guard
        has_active_label=False,
    )
    snapshot = Snapshot(prs=(pr,))
    actions = decide_cycle(snapshot, _CONFIG, _NOW)
    assert actions == []


def test_pitfall7__empty_queues_prevents_vacuous_headship() -> None:
    """Pitfall 7: is_head_of_all returns False for frozenset() (not vacuously True)."""
    # A PR with empty queues must NEVER receive any action, even if it
    # passes all other filters. The is_head_of_all guard explicitly returns
    # False when pr.queues is empty to block the vacuous all([]) == True bug.
    pr_no_queues = _make_pr_state(
        9,
        frozenset(),
        has_active_label=True,
        is_validly_active=True,
        required_check_results=_all_passed_checks(),
    )
    snapshot = Snapshot(prs=(pr_no_queues,))
    actions = decide_cycle(snapshot, _CONFIG, _NOW)
    assert actions == [], (
        "PR with queues=frozenset() must never receive any action (Pitfall 7 guard)"
    )


# ---------------------------------------------------------------------------
# Bonus: 5a continue enforcement
# ---------------------------------------------------------------------------


def test_bonus__5a_continue_activate_only_never_two_actions_for_same_pr() -> None:
    """5a continue: Activate is the only action for a ready PR with no mq:active.

    Even when is_validly_active=True and all checks pass, a PR without mq:active
    must only receive Activate — never Activate+Squash. The 5b branch is guarded
    by the 'mq:active not in labels' check, and the continue in 5a ensures
    no further dispatch happens for the same PR in the same cycle.
    """
    pr = _make_pr_state(
        10,
        frozenset({"hipdnn"}),
        has_active_label=False,  # no mq:active → 5a fires
        is_validly_active=True,  # would pass 5b if active label were present
        required_check_results=_all_passed_checks(),  # would Squash if past 5b
    )
    snapshot = Snapshot(prs=(pr,))
    actions = decide_cycle(snapshot, _CONFIG, _NOW)
    assert len(actions) == 1
    assert isinstance(actions[0], Activate)
    assert actions[0].pr.number == 10


# ---------------------------------------------------------------------------
# Bonus: Q2 resolution — disjoint-queue PRs can be [Squash(A), Activate(B)]
# in same cycle
# ---------------------------------------------------------------------------


def test_bonus__q2_resolution__disjoint_queues_squash_and_activate_same_cycle() -> None:
    """Q2 resolution: [Squash(A), Activate(B)] is valid for disjoint-queue PRs.

    RESEARCH.md Open Question 2 resolution: "Activation and evaluation never happen
    in the same cycle for the same PR" is a PER-PR invariant, NOT a per-cycle-global
    invariant. When PRs A and B belong to DISJOINT queues and are both at the head
    of their respective queues, the algorithm evaluates each independently:
      - A has mq:active + valid + checks pass → Squash(A)
      - B has mq:queued (no mq:active) → Activate(B)

    Both actions appear in the same cycle's output. This is correct behavior:
    A is in a different cycle stage (5b) from B (5a). The "no same-cycle activation
    AND evaluation" rule applies per-PR: A never gets both Activate AND Squash
    in the same cycle; B never gets both Activate AND Squash in the same cycle.
    """
    # PR A: in hipdnn queue only, ready to Squash
    pr_a = _make_pr_state(
        100,
        frozenset({"hipdnn"}),
        has_active_label=True,
        is_validly_active=True,
        required_check_results=_all_passed_checks(),
        enqueued_at=_T1,
    )
    # PR B: in miopen-provider queue only (disjoint from A), ready to Activate
    pr_b = _make_pr_state(
        200,
        frozenset({"miopen-provider"}),
        has_active_label=False,
        enqueued_at=_T0,
    )

    snapshot = Snapshot(prs=(pr_a, pr_b))
    actions = decide_cycle(snapshot, _CONFIG, _NOW)

    # Both actions must be present — order may vary across disjoint queue sets
    assert len(actions) == 2
    action_types = {type(a).__name__ for a in actions}
    assert "Squash" in action_types
    assert "Activate" in action_types

    # Verify the right PR got each action
    squash_prs = [a.pr.number for a in actions if isinstance(a, Squash)]
    activate_prs = [a.pr.number for a in actions if isinstance(a, Activate)]
    assert squash_prs == [100]
    assert activate_prs == [200]

    # Verify per-PR invariant: A never got Activate; B never got Squash
    for action in actions:
        if isinstance(action, Squash):
            assert action.pr.number != 200, "B must not be Squashed in same cycle"
        if isinstance(action, Activate):
            assert action.pr.number != 100, "A must not be Activated in same cycle"


# ---------------------------------------------------------------------------
# Edge: no-required-checks PR with active label → Squash (zero results is all_passed)
# ---------------------------------------------------------------------------


def test_edge__no_required_checks_active_valid_squash() -> None:
    """PR with zero required_check_results + valid active → Squash (no checks = all_passed)."""
    pr = _make_pr_state(
        11,
        frozenset({"hipdnn"}),
        has_active_label=True,
        is_validly_active=True,
        required_check_results=(),  # zero results
    )
    snapshot = Snapshot(prs=(pr,))
    actions = decide_cycle(snapshot, _CONFIG, _NOW)
    assert actions == [Squash(pr=pr)]


# ---------------------------------------------------------------------------
# Edge: mixed failed+pending checks → Eject (any_failed takes priority)
# ---------------------------------------------------------------------------


def test_edge__mixed_failed_and_pending_emits_eject() -> None:
    """any_failed takes priority over pending: Eject with name of failed check."""
    pr = _make_pr_state(
        12,
        frozenset({"hipdnn"}),
        has_active_label=True,
        is_validly_active=True,
        required_check_results=(
            RequiredCheckResult(name="CI / lint", state="pending"),
            RequiredCheckResult(name="CI / test", state="failure"),
        ),
    )
    snapshot = Snapshot(prs=(pr,))
    actions = decide_cycle(snapshot, _CONFIG, _NOW)
    assert len(actions) == 1
    eject = actions[0]
    assert isinstance(eject, Eject)
    assert "CI / test" in eject.reason
