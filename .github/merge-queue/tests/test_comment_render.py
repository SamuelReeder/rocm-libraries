"""
tests/test_comment_render.py — Syrupy snapshot tests for comment.render_status_body.

Coverage: PURE-04 part 1 — status comment rendering for all four PR states.

Snapshot update: ``pytest tests/test_comment_render.py --snapshot-update``
Always review the .ambr diff before committing — the snapshot IS the contract.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from syrupy.assertion import SnapshotAssertion

from rocm_mq.comment import render_status_body

# ---------------------------------------------------------------------------
# Fixed timestamp used across all snapshot tests
# ---------------------------------------------------------------------------

FIXED_NOW = datetime(2026, 4, 22, 15, 26, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Parametrized syrupy snapshot tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "state,fixture_suffix",
    [
        ("queued", "miopen_pr_queued_position_2_of_3"),
        ("active", "core_pr_active_checks_pending"),
        ("merged", "core_pr_merged"),
        ("ejected_ci", "core_pr_ejected_ci_failure"),
        ("ejected_revoked", "core_pr_ejected_approval_revoked"),
    ],
)
def test_render_status_body_snapshot(
    state: str,
    fixture_suffix: str,
    snapshot: SnapshotAssertion,
    request: pytest.FixtureRequest,
) -> None:
    """Golden-file snapshot test per PR state.

    Each case generates a markdown body and compares it to the committed
    .ambr golden.  Drift in the rendered output (including the
    <!-- rocm-mq-status --> marker) surfaces as a test failure — reviewer
    MUST diff the .ambr change before approving a PR.
    """
    pr_state = request.getfixturevalue(f"pr_state__{fixture_suffix}")
    render_ctx = request.getfixturevalue(f"render_ctx__{fixture_suffix}")
    result = render_status_body(pr_state, render_ctx, FIXED_NOW)
    assert result == snapshot


# ---------------------------------------------------------------------------
# Non-snapshot smoke tests — invariants that MUST hold for every rendered body
# ---------------------------------------------------------------------------


def test_every_state_contains_marker(
    pr_state__miopen_pr_queued_position_2_of_3: object,
    render_ctx__miopen_pr_queued_position_2_of_3: object,
    pr_state__core_pr_active_checks_pending: object,
    render_ctx__core_pr_active_checks_pending: object,
    pr_state__core_pr_merged: object,
    render_ctx__core_pr_merged: object,
    pr_state__core_pr_ejected_ci_failure: object,
    render_ctx__core_pr_ejected_ci_failure: object,
    pr_state__core_pr_ejected_approval_revoked: object,
    render_ctx__core_pr_ejected_approval_revoked: object,
) -> None:
    """Every rendered body must contain the load-bearing HTML marker."""
    from rocm_mq.state import PRState, RenderContext

    scenarios = [
        (pr_state__miopen_pr_queued_position_2_of_3, render_ctx__miopen_pr_queued_position_2_of_3),
        (pr_state__core_pr_active_checks_pending, render_ctx__core_pr_active_checks_pending),
        (pr_state__core_pr_merged, render_ctx__core_pr_merged),
        (pr_state__core_pr_ejected_ci_failure, render_ctx__core_pr_ejected_ci_failure),
        (pr_state__core_pr_ejected_approval_revoked, render_ctx__core_pr_ejected_approval_revoked),
    ]
    for pr_state, render_ctx in scenarios:
        assert isinstance(pr_state, PRState)
        assert isinstance(render_ctx, RenderContext)
        body = render_status_body(pr_state, render_ctx, FIXED_NOW)
        assert "<!-- rocm-mq-status -->" in body, (
            f"Marker missing for state={render_ctx.state}"
        )


def test_queued_contains_dequeue_instruction(
    pr_state__miopen_pr_queued_position_2_of_3: object,
    render_ctx__miopen_pr_queued_position_2_of_3: object,
) -> None:
    """/dequeue instruction must appear in queued body."""
    from rocm_mq.state import PRState, RenderContext

    assert isinstance(pr_state__miopen_pr_queued_position_2_of_3, PRState)
    assert isinstance(render_ctx__miopen_pr_queued_position_2_of_3, RenderContext)
    body = render_status_body(
        pr_state__miopen_pr_queued_position_2_of_3,
        render_ctx__miopen_pr_queued_position_2_of_3,
        FIXED_NOW,
    )
    assert "/dequeue" in body


def test_ejected_contains_reenqueue_instruction(
    pr_state__core_pr_ejected_ci_failure: object,
    render_ctx__core_pr_ejected_ci_failure: object,
) -> None:
    """/merge re-enqueue instruction must appear in ejected body."""
    from rocm_mq.state import PRState, RenderContext

    assert isinstance(pr_state__core_pr_ejected_ci_failure, PRState)
    assert isinstance(render_ctx__core_pr_ejected_ci_failure, RenderContext)
    body = render_status_body(
        pr_state__core_pr_ejected_ci_failure,
        render_ctx__core_pr_ejected_ci_failure,
        FIXED_NOW,
    )
    assert "/merge" in body
    assert "Re-enqueue" in body


def test_merged_contains_merged_sha(
    pr_state__core_pr_merged: object,
    render_ctx__core_pr_merged: object,
) -> None:
    """Merged body must contain the merged_sha."""
    from rocm_mq.state import PRState, RenderContext

    assert isinstance(pr_state__core_pr_merged, PRState)
    assert isinstance(render_ctx__core_pr_merged, RenderContext)
    body = render_status_body(
        pr_state__core_pr_merged,
        render_ctx__core_pr_merged,
        FIXED_NOW,
    )
    assert render_ctx__core_pr_merged.merged_sha in body


def test_cycle_run_url_appears_when_set(
    pr_state__core_pr_active_checks_pending: object,
    render_ctx__core_pr_active_checks_pending: object,
) -> None:
    """When cycle_run_url is not None, the 'Last processed' line appears."""
    from rocm_mq.state import PRState, RenderContext

    assert isinstance(pr_state__core_pr_active_checks_pending, PRState)
    assert isinstance(render_ctx__core_pr_active_checks_pending, RenderContext)
    render_ctx = render_ctx__core_pr_active_checks_pending
    assert render_ctx.cycle_run_url is not None, "fixture must have a URL for this test"
    body = render_status_body(
        pr_state__core_pr_active_checks_pending,
        render_ctx,
        FIXED_NOW,
    )
    assert "Last processed:" in body
    assert render_ctx.cycle_run_url in body


def test_unknown_state_raises_value_error(
    pr_state__miopen_pr_queued_position_2_of_3: object,
    render_ctx__miopen_pr_queued_position_2_of_3: object,
) -> None:
    """An unknown state string raises ValueError containing 'unknown state'."""
    from dataclasses import replace

    from rocm_mq.state import PRState, RenderContext

    assert isinstance(pr_state__miopen_pr_queued_position_2_of_3, PRState)
    assert isinstance(render_ctx__miopen_pr_queued_position_2_of_3, RenderContext)
    bad_ctx = replace(render_ctx__miopen_pr_queued_position_2_of_3, state="frobnicate")
    with pytest.raises(ValueError, match="unknown state"):
        render_status_body(
            pr_state__miopen_pr_queued_position_2_of_3,
            bad_ctx,
            FIXED_NOW,
        )


# ---------------------------------------------------------------------------
# canonical_merge_queue_config smoke tests (contract verification)
# ---------------------------------------------------------------------------


def test_canonical_merge_queue_config_importable() -> None:
    """canonical_merge_queue_config is importable from tests.conftest."""
    from tests.conftest import canonical_merge_queue_config

    cfg = canonical_merge_queue_config()
    expected_queues = {
        "hipdnn",
        "miopen-provider",
        "hipblaslt-provider",
        "composable-kernel-provider",
        "rocblas-provider",
        "integration-tests",
    }
    assert expected_queues.issubset(cfg.all_queues)
    assert cfg.activation_status_context == "merge-queue/active"
    assert cfg.active_label == "mq:active"
    assert cfg.queued_label == "mq:queued"


def test_canonical_app_identity_matches_config() -> None:
    """CANONICAL_APP constant matches canonical_merge_queue_config().app_identity."""
    from tests.conftest import CANONICAL_APP, canonical_merge_queue_config

    cfg = canonical_merge_queue_config()
    assert cfg.app_identity == CANONICAL_APP


def test_canonical_app_identity_fixture(canonical_app_identity: object) -> None:
    """canonical_app_identity fixture returns CANONICAL_APP."""
    from rocm_mq.state import AppIdentity
    from tests.conftest import CANONICAL_APP

    assert isinstance(canonical_app_identity, AppIdentity)
    assert canonical_app_identity == CANONICAL_APP
