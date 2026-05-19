"""
tests/test_summary_render.py — Syrupy snapshot tests for summary.render_cycle_summary.

Coverage: PURE-04 part 2 — cycle summary rendering for empty, mixed, and all-Defer cycles.

Snapshot update: ``pytest tests/test_summary_render.py --snapshot-update``
Always review the .ambr diff before committing — the snapshot IS the contract.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from syrupy.assertion import SnapshotAssertion

from rocm_mq.state import (
    ActionOutcome,
    Activate,
    CycleRenderContext,
    Defer,
    Eject,
    PartialPRState,
    PRState,
    RequiredCheckResult,
    Snapshot,
    Squash,
    UpdateComment,
)
from rocm_mq.summary import render_cycle_summary


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    """Local tz-aware helper for fixture construction."""
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Shared fixtures for cycle scenarios
# ---------------------------------------------------------------------------


def _make_pr(
    number: int,
    labels: frozenset[str],
    enqueued_at: datetime,
    head_sha: str = "a" * 40,
    is_validly_active: bool = False,
    checks: tuple[tuple[str, str], ...] = (),
) -> PRState:
    return PRState(
        number=number,
        head_sha=head_sha,
        labels=labels,
        queues=frozenset(),  # queues not relevant for renderer
        enqueued_at=enqueued_at,
        is_validly_active=is_validly_active,
        required_check_results=tuple(RequiredCheckResult(n, s) for n, s in checks),
    )


# ---------------------------------------------------------------------------
# Scenario 1: Empty cycle
# ---------------------------------------------------------------------------


def _empty_cycle_ctx() -> CycleRenderContext:
    return CycleRenderContext(
        cycle_started_at=utc(2026, 4, 22, 14, 0),
        cycle_completed_at=utc(2026, 4, 22, 14, 0, 30),
        cycle_run_url="https://github.com/SamuelReeder/rocm-libraries/actions/runs/999",
        queue_depths=(
            ("hipdnn", 0),
            ("miopen-provider", 0),
            ("hipblaslt-provider", 0),
            ("composable-kernel-provider", 0),
            ("rocblas-provider", 0),
            ("integration-tests", 0),
        ),
    )


# ---------------------------------------------------------------------------
# Scenario 2: Mixed cycle
# (5 PRs in snapshot: 2 active, 3 queued; 4 actions with one failed outcome)
# ---------------------------------------------------------------------------


def _mixed_cycle_snapshot() -> Snapshot:
    pr1 = _make_pr(
        10,
        frozenset({"mq:queued", "mq:active", "mq:hipdnn"}),
        utc(2026, 4, 22, 8, 0),
        head_sha="a" * 7 + "1234567890123456789012345678901234",
        is_validly_active=True,
        checks=(("TheRock / build", "success"),),
    )
    pr2 = _make_pr(
        20,
        frozenset({"mq:queued", "mq:active", "mq:miopen-provider"}),
        utc(2026, 4, 22, 9, 0),
        head_sha="b" * 40,
        is_validly_active=False,
    )
    pr3 = _make_pr(
        30,
        frozenset({"mq:queued", "mq:hipblaslt-provider"}),
        utc(2026, 4, 22, 10, 0),
        head_sha="c" * 40,
    )
    pr4 = _make_pr(
        40,
        frozenset({"mq:queued", "mq:composable-kernel-provider"}),
        utc(2026, 4, 22, 11, 0),
        head_sha="d" * 40,
    )
    pr5 = _make_pr(
        50,
        frozenset({"mq:queued", "mq:rocblas-provider"}),
        utc(2026, 4, 22, 12, 0),
        head_sha="e" * 40,
    )
    return Snapshot(prs=(pr1, pr2, pr3, pr4, pr5))


def _mixed_cycle_actions_and_outcomes() -> (
    tuple[
        tuple[Squash | Activate | Eject | Defer, ...],
        tuple[ActionOutcome, ...],
    ]
):
    pr1 = _mixed_cycle_snapshot().prs[0]
    pr2 = _mixed_cycle_snapshot().prs[1]
    pr3 = _mixed_cycle_snapshot().prs[2]
    pr4 = _mixed_cycle_snapshot().prs[3]
    actions: tuple[Squash | Activate | Eject | Defer, ...] = (
        Squash(pr=pr1),
        Activate(pr=pr2),
        Eject(pr=pr3, reason="TheRock CI failed"),
        Defer(
            pr=PartialPRState(number=pr4.number, head_sha=pr4.head_sha, labels=pr4.labels),
            reason="timeline lag",
        ),
    )
    outcomes: tuple[ActionOutcome, ...] = (
        ActionOutcome(action=actions[0], success=True, error_message=None),
        ActionOutcome(action=actions[1], success=True, error_message=None),
        ActionOutcome(action=actions[2], success=False, error_message="branch protection blocked"),
        ActionOutcome(action=actions[3], success=True, error_message=None),
    )
    return actions, outcomes


def _mixed_cycle_ctx() -> CycleRenderContext:
    return CycleRenderContext(
        cycle_started_at=utc(2026, 4, 22, 14, 0),
        cycle_completed_at=utc(2026, 4, 22, 14, 0, 30),
        cycle_run_url="https://github.com/SamuelReeder/rocm-libraries/actions/runs/1000",
        queue_depths=(
            ("hipdnn", 1),
            ("miopen-provider", 1),
            ("hipblaslt-provider", 1),
            ("composable-kernel-provider", 1),
            ("rocblas-provider", 1),
            ("integration-tests", 0),
        ),
    )


# ---------------------------------------------------------------------------
# Scenario 3: All-Defer cycle
# ---------------------------------------------------------------------------


def _all_defer_snapshot() -> Snapshot:
    pr1 = _make_pr(11, frozenset({"mq:queued", "mq:hipdnn"}), utc(2026, 4, 22, 7, 0))
    pr2 = _make_pr(12, frozenset({"mq:queued", "mq:miopen-provider"}), utc(2026, 4, 22, 7, 30))
    pr3 = _make_pr(13, frozenset({"mq:queued", "mq:rocblas-provider"}), utc(2026, 4, 22, 8, 0))
    return Snapshot(prs=(pr1, pr2, pr3))


def _all_defer_actions_and_outcomes() -> (
    tuple[tuple[Defer, ...], tuple[ActionOutcome, ...]]
):
    s = _all_defer_snapshot()
    actions: tuple[Defer, ...] = (
        Defer(
            pr=PartialPRState(
                number=s.prs[0].number,
                head_sha=s.prs[0].head_sha,
                labels=s.prs[0].labels,
            ),
            reason="timeline lag",
        ),
        Defer(
            pr=PartialPRState(
                number=s.prs[1].number,
                head_sha=s.prs[1].head_sha,
                labels=s.prs[1].labels,
            ),
            reason="mq:queued label present but timeline event not yet visible",
        ),
        Defer(
            pr=PartialPRState(
                number=s.prs[2].number,
                head_sha=s.prs[2].head_sha,
                labels=s.prs[2].labels,
            ),
            reason="mq:queued applied by non-App actor",
        ),
    )
    outcomes: tuple[ActionOutcome, ...] = tuple(
        ActionOutcome(action=a, success=True, error_message=None) for a in actions
    )
    return actions, outcomes


def _all_defer_ctx() -> CycleRenderContext:
    return CycleRenderContext(
        cycle_started_at=utc(2026, 4, 22, 14, 0),
        cycle_completed_at=utc(2026, 4, 22, 14, 0, 15),
        cycle_run_url="https://github.com/SamuelReeder/rocm-libraries/actions/runs/1001",
        queue_depths=(
            ("hipdnn", 3),
            ("miopen-provider", 1),
            ("rocblas-provider", 1),
        ),
    )


# ---------------------------------------------------------------------------
# Snapshot tests
# ---------------------------------------------------------------------------


def test_render_cycle_summary_snapshot_empty(snapshot: SnapshotAssertion) -> None:
    """Empty cycle snapshot: no actions, no active PRs, all depths zero."""
    result = render_cycle_summary(
        Snapshot(prs=()),
        (),
        (),
        _empty_cycle_ctx(),
    )
    assert result == snapshot


def test_render_cycle_summary_snapshot_mixed(snapshot: SnapshotAssertion) -> None:
    """Mixed cycle snapshot: Squash + Activate + Eject (failed) + Defer."""
    actions, outcomes = _mixed_cycle_actions_and_outcomes()
    result = render_cycle_summary(
        _mixed_cycle_snapshot(),
        actions,
        outcomes,
        _mixed_cycle_ctx(),
    )
    assert result == snapshot


def test_render_cycle_summary_snapshot_all_defer(snapshot: SnapshotAssertion) -> None:
    """All-Defer cycle snapshot: three Defer actions, no active PRs."""
    actions, outcomes = _all_defer_actions_and_outcomes()
    result = render_cycle_summary(
        _all_defer_snapshot(),
        actions,
        outcomes,
        _all_defer_ctx(),
    )
    assert result == snapshot


# ---------------------------------------------------------------------------
# Non-snapshot smoke tests
# ---------------------------------------------------------------------------


def test_section_headers_appear_in_order() -> None:
    """All four section headers appear in the rendered output in fixed order."""
    result = render_cycle_summary(Snapshot(prs=()), (), (), _empty_cycle_ctx())
    headers = ["## Queue depth", "## Active PRs", "## Cycle outcomes", "## Cycle duration"]
    positions = [result.index(h) for h in headers]
    assert positions == sorted(positions), "Section headers not in expected order"


def test_mismatched_lengths_raises_value_error() -> None:
    """Mismatched actions/outcomes lengths raises ValueError."""
    pr = _make_pr(1, frozenset({"mq:queued"}), utc(2026, 1, 1))
    with pytest.raises(ValueError, match="length mismatch"):
        render_cycle_summary(
            Snapshot(prs=(pr,)),
            (Activate(pr=pr),),  # 1 action
            (),  # 0 outcomes
            _empty_cycle_ctx(),
        )


def test_no_actions_renders_placeholder() -> None:
    """No actions this cycle renders '_No actions this cycle._' placeholder."""
    result = render_cycle_summary(Snapshot(prs=()), (), (), _empty_cycle_ctx())
    assert "_No actions this cycle._" in result


def test_failed_outcome_appends_failed_suffix() -> None:
    """A failed outcome appends '— FAILED: <message>' to the action line."""
    pr = _make_pr(7, frozenset({"mq:queued", "mq:hipdnn"}), utc(2026, 4, 1))
    action = Activate(pr=pr)
    outcome = ActionOutcome(action=action, success=False, error_message="API rate limit")
    ctx = CycleRenderContext(
        cycle_started_at=utc(2026, 4, 1, 10, 0),
        cycle_completed_at=utc(2026, 4, 1, 10, 0, 5),
        cycle_run_url=None,
        queue_depths=(("hipdnn", 1),),
    )
    result = render_cycle_summary(Snapshot(prs=(pr,)), (action,), (outcome,), ctx)
    assert "FAILED: API rate limit" in result


def test_exhaustiveness_all_action_variants() -> None:
    """Every Action variant produces the expected emoji prefix in the output."""
    pr = _make_pr(99, frozenset({"mq:queued", "mq:hipdnn"}), utc(2026, 4, 1))
    partial = PartialPRState(number=98, head_sha="x" * 40, labels=frozenset({"mq:queued"}))
    actions = (
        Activate(pr=pr),
        Squash(pr=pr),
        Eject(pr=pr, reason="test reason"),
        UpdateComment(pr=pr, new_body="new body"),
        Defer(pr=partial, reason="lag"),
    )
    outcomes = tuple(
        ActionOutcome(action=a, success=True, error_message=None) for a in actions
    )
    ctx = CycleRenderContext(
        cycle_started_at=utc(2026, 4, 1, 10, 0),
        cycle_completed_at=utc(2026, 4, 1, 10, 0, 5),
        cycle_run_url=None,
        queue_depths=(("hipdnn", 1),),
    )
    result = render_cycle_summary(Snapshot(prs=(pr,)), actions, outcomes, ctx)
    # Each variant must produce its expected emoji + PR number
    assert "✨" in result and "#99" in result  # Activate
    assert "✅" in result  # Squash
    assert "❌" in result  # Eject
    assert "💬" in result  # UpdateComment
    assert "⏸" in result  # Defer


def test_cycle_run_url_appears_in_footer() -> None:
    """When cycle_run_url is set, a link to the run appears in the output."""
    result = render_cycle_summary(Snapshot(prs=()), (), (), _empty_cycle_ctx())
    ctx = _empty_cycle_ctx()
    assert ctx.cycle_run_url in result


def test_no_banned_imports_in_summary_module() -> None:
    """summary.py must not call datetime.now, datetime.utcnow, or datetime.fromisoformat."""
    import ast
    import pathlib

    src = pathlib.Path(__file__).parent.parent / "src" / "rocm_mq" / "summary.py"
    tree = ast.parse(src.read_text())
    banned_methods = {"now", "utcnow", "fromisoformat"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in banned_methods:
            pytest.fail(
                f"summary.py uses banned datetime method: {node.attr} (PURE-09 violation)"
            )
