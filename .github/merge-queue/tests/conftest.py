"""
tests/conftest.py — Shared test scaffolding for the rocm_mq test suite.

Responsibilities:
1. Hypothesis profile registration (ci: 500 examples, dev: 100 examples).
   Profile is selected by the ``CI`` environment variable.
2. ``utc(year, month, day, ...)`` datetime helper — the ONLY way tests should
   construct tz-aware datetimes (Pitfall 3 chokepoint for test code).
3. Canonical ``AppIdentity`` fixture + five non-canonical ``CommitStatusCreator``
   fixtures (RESEARCH.md *Identity Helper* fixture matrix lines 384-391).
4. Skeleton per-state PR + RenderContext fixtures (populated with real content
   in Plan 03 renderer tests; these are placeholders for Plan 01 scaffolding).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from hypothesis import HealthCheck, settings

from rocm_mq.state import (
    AppIdentity,
    CommitStatusCreator,
    PRState,
    RenderContext,
    RequiredCheckResult,
)

# ---------------------------------------------------------------------------
# Hypothesis profile registration
# ---------------------------------------------------------------------------

settings.register_profile(
    "ci",
    max_examples=500,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)

settings.register_profile(
    "dev",
    max_examples=100,
    deadline=200,
)

# Select profile: CI env var set → ci profile; otherwise → dev profile
if os.environ.get("CI"):
    settings.load_profile("ci")
else:
    settings.load_profile("dev")


# ---------------------------------------------------------------------------
# tz-aware datetime helper — the only way test code should construct datetimes
# (Pitfall 3: forgetting tz=timezone.utc leads to naive-datetime FIFO corruption)
# ---------------------------------------------------------------------------


def utc(
    year: int,
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
    second: int = 0,
    microsecond: int = 0,
) -> datetime:
    """Return a tz-aware datetime at UTC.

    The ONLY approved way to construct datetimes in test code. Never call
    ``datetime(...)`` directly without ``tzinfo=timezone.utc`` in tests.
    """
    return datetime(year, month, day, hour, minute, second, microsecond, tzinfo=UTC)


# ---------------------------------------------------------------------------
# AppIdentity fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def canonical_app_identity() -> AppIdentity:
    """The canonical merge-queue App identity (sentinel values for tests)."""
    return AppIdentity(slug="rocm-mq", app_id=12345, bot_user_id=99999)


# ---------------------------------------------------------------------------
# CommitStatusCreator fixtures (canonical + 5 non-canonical variants)
# (RESEARCH.md *Identity Helper* fixture matrix lines 384-391)
# ---------------------------------------------------------------------------


@pytest.fixture()
def creator_canonical() -> CommitStatusCreator:
    """The canonical App creator — is_app_identity should return True."""
    return CommitStatusCreator(
        login="rocm-mq[bot]",
        type="Bot",
        app_slug="rocm-mq",
        app_id=12345,
    )


@pytest.fixture()
def creator_wrong_slug() -> CommitStatusCreator:
    """Wrong slug (typo) — is_app_identity must return False."""
    return CommitStatusCreator(
        login="rocm-mq[bot]",
        type="Bot",
        app_slug="rocm-mp",  # typo
        app_id=12345,
    )


@pytest.fixture()
def creator_wrong_id() -> CommitStatusCreator:
    """Right slug but different app_id — is_app_identity must return False."""
    return CommitStatusCreator(
        login="rocm-mq[bot]",
        type="Bot",
        app_slug="rocm-mq",
        app_id=54321,  # different App, same slug
    )


@pytest.fixture()
def creator_user_type() -> CommitStatusCreator:
    """Right slug+ID but type='User' — is_app_identity must return False."""
    return CommitStatusCreator(
        login="rocm-mq",
        type="User",  # impersonator account
        app_slug="rocm-mq",
        app_id=12345,
    )


@pytest.fixture()
def creator_sibling_workflow() -> CommitStatusCreator:
    """github-actions[bot] (sibling workflow with GITHUB_TOKEN) — must return False."""
    return CommitStatusCreator(
        login="github-actions[bot]",
        type="Bot",
        app_slug="github-actions",
        app_id=15368,  # GitHub Actions App ID
    )


@pytest.fixture()
def creator_bare_bot_no_app() -> CommitStatusCreator:
    """Bot type but no app sub-object (app_slug=None, app_id=None) — must return False."""
    return CommitStatusCreator(
        login="rocm-mq[bot]",
        type="Bot",
        app_slug=None,
        app_id=None,
    )


# ---------------------------------------------------------------------------
# Convenience fixture: all non-canonical creators as a parametrized list
# (used by test_helpers_is_app_identity.py)
# ---------------------------------------------------------------------------


@pytest.fixture(
    params=[
        "wrong_slug",
        "wrong_id",
        "user_type",
        "sibling_workflow",
        "bare_bot_no_app",
    ]
)
def non_canonical_creator(
    request: pytest.FixtureRequest,
    creator_wrong_slug: CommitStatusCreator,
    creator_wrong_id: CommitStatusCreator,
    creator_user_type: CommitStatusCreator,
    creator_sibling_workflow: CommitStatusCreator,
    creator_bare_bot_no_app: CommitStatusCreator,
) -> CommitStatusCreator:
    """Parametrized fixture yielding each non-canonical creator variant."""
    mapping = {
        "wrong_slug": creator_wrong_slug,
        "wrong_id": creator_wrong_id,
        "user_type": creator_user_type,
        "sibling_workflow": creator_sibling_workflow,
        "bare_bot_no_app": creator_bare_bot_no_app,
    }
    return mapping[request.param]


# ---------------------------------------------------------------------------
# Skeleton per-state PR fixtures
# (Plan 03 renderer tests will populate with real RenderContext content;
#  these are minimal placeholders exercising the dataclass construction path)
# ---------------------------------------------------------------------------


def _make_required_checks(
    *names_states: tuple[str, str],
) -> tuple[RequiredCheckResult, ...]:
    return tuple(RequiredCheckResult(name=n, state=s) for n, s in names_states)


@pytest.fixture()
def pr_state__miopen_pr_queued_position_2_of_3() -> PRState:
    """MIOpen PR queued at position 2 of 3 in the hipdnn queue."""
    return PRState(
        number=101,
        head_sha="abc1111",
        labels=frozenset({"mq:queued"}),
        queues=frozenset({"hipdnn"}),
        enqueued_at=utc(2026, 4, 22, 10, 0, 0),
        is_validly_active=False,
        required_check_results=_make_required_checks(("CI / build", "pending")),
    )


@pytest.fixture()
def pr_state__core_pr_active_checks_pending() -> PRState:
    """Core PR at head of queue with activation status set but checks still pending."""
    return PRState(
        number=201,
        head_sha="def2222",
        labels=frozenset({"mq:queued", "mq:active"}),
        queues=frozenset({"core", "hipdnn"}),
        enqueued_at=utc(2026, 4, 22, 9, 0, 0),
        is_validly_active=True,
        required_check_results=_make_required_checks(
            ("CI / build", "pending"),
            ("CI / test", "pending"),
        ),
    )


@pytest.fixture()
def pr_state__core_pr_merged() -> PRState:
    """Core PR that has been squash-merged (labels cleared)."""
    return PRState(
        number=202,
        head_sha="ghi3333",
        labels=frozenset(),
        queues=frozenset(),
        enqueued_at=utc(2026, 4, 22, 8, 0, 0),
        is_validly_active=False,
        required_check_results=_make_required_checks(
            ("CI / build", "success"),
            ("CI / test", "success"),
        ),
    )


@pytest.fixture()
def pr_state__core_pr_ejected_ci_failure() -> PRState:
    """Core PR ejected because required CI check failed."""
    return PRState(
        number=203,
        head_sha="jkl4444",
        labels=frozenset({"mq:queued", "mq:active"}),
        queues=frozenset({"core"}),
        enqueued_at=utc(2026, 4, 22, 7, 30, 0),
        is_validly_active=True,
        required_check_results=_make_required_checks(
            ("CI / build", "failure"),
            ("CI / test", "pending"),
        ),
    )


@pytest.fixture()
def pr_state__core_pr_ejected_approval_revoked() -> PRState:
    """Core PR that had approval revoked after enqueue."""
    return PRState(
        number=204,
        head_sha="mno5555",
        labels=frozenset({"mq:queued"}),
        queues=frozenset({"core"}),
        enqueued_at=utc(2026, 4, 22, 7, 0, 0),
        is_validly_active=False,
        required_check_results=_make_required_checks(("CI / build", "success")),
    )


# ---------------------------------------------------------------------------
# Skeleton RenderContext fixtures (parallel to per-state PR fixtures above)
# ---------------------------------------------------------------------------


@pytest.fixture()
def render_ctx__miopen_pr_queued_position_2_of_3() -> RenderContext:
    """RenderContext for a queued PR at position 2 of 3."""
    return RenderContext(
        author_login="dev-user",
        pr_title="[MIOpen] Add SDPA kernel variant",
        queue_positions=(("hipdnn", 2, 3),),
        blockers=(100,),  # PR 100 is ahead in queue
        cycle_run_url=None,
        state="queued",
        eject_reason=None,
        merged_sha=None,
    )


@pytest.fixture()
def render_ctx__core_pr_active_checks_pending() -> RenderContext:
    """RenderContext for a PR that is active (head-of-queue) but checks pending."""
    return RenderContext(
        author_login="core-dev",
        pr_title="[Core] Refactor memory allocator",
        queue_positions=(("core", 1, 2), ("hipdnn", 1, 2)),
        blockers=(),
        cycle_run_url="https://github.com/ROCm/rocm-libraries/actions/runs/12345",
        state="active",
        eject_reason=None,
        merged_sha=None,
    )


@pytest.fixture()
def render_ctx__core_pr_merged() -> RenderContext:
    """RenderContext for a merged PR."""
    return RenderContext(
        author_login="core-dev",
        pr_title="[Core] Fix memory leak in graph mode",
        queue_positions=(),
        blockers=(),
        cycle_run_url="https://github.com/ROCm/rocm-libraries/actions/runs/12344",
        state="merged",
        eject_reason=None,
        merged_sha="deadbeef1234567890abcdef",
    )


@pytest.fixture()
def render_ctx__core_pr_ejected_ci_failure() -> RenderContext:
    """RenderContext for a CI-failure ejected PR."""
    return RenderContext(
        author_login="core-dev",
        pr_title="[Core] Add experimental feature",
        queue_positions=(),
        blockers=(),
        cycle_run_url="https://github.com/ROCm/rocm-libraries/actions/runs/12343",
        state="ejected",
        eject_reason="Required check 'CI / build' failed",
        merged_sha=None,
    )


@pytest.fixture()
def render_ctx__core_pr_ejected_approval_revoked() -> RenderContext:
    """RenderContext for an approval-revoked ejected PR."""
    return RenderContext(
        author_login="core-dev",
        pr_title="[Core] Update dependency pins",
        queue_positions=(),
        blockers=(),
        cycle_run_url=None,
        state="ejected",
        eject_reason="Required approval was revoked after enqueue",
        merged_sha=None,
    )
