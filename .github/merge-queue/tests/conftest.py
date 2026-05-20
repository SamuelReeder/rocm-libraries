"""
tests/conftest.py — Shared test scaffolding for the rocm_mq test suite.

Responsibilities:
1. Hypothesis profile registration (ci: 500 examples, dev: 100 examples).
   Profile is selected by the ``CI`` environment variable.
2. ``utc(year, month, day, ...)`` datetime helper — the ONLY way tests should
   construct tz-aware datetimes (Pitfall 3 chokepoint for test code).
3. Canonical ``AppIdentity`` constant + fixture + five non-canonical
   ``CommitStatusCreator`` fixtures (RESEARCH.md *Identity Helper* fixture
   matrix lines 384-391).
4. ``CANONICAL_APP`` module-level constant and ``canonical_merge_queue_config()``
   callable — the single owning home for the canonical MergeQueueConfig consumed
   by Plans 04 (invariant suite) and 05 (worked-example regression).
   Neither downstream plan redefines this symbol; both import from here only.
5. Per-state PR + RenderContext fixtures for renderer snapshot tests (Plan 03).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from hypothesis import HealthCheck, settings

from rocm_mq.state import (
    AppIdentity,
    CommitStatusCreator,
    MergeQueueConfig,
    PRState,
    RenderContext,
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
# CANONICAL_APP — module-level constant (Plan 03 territory, Plan 04 + 05 import)
#
# This is the single owning home of the canonical App identity sentinel.
# Plan 04 invariant suite and Plan 05 worked example import CANONICAL_APP from
# here; neither redefines it.
# ---------------------------------------------------------------------------

CANONICAL_APP = AppIdentity(slug="rocm-mq", app_id=12345, bot_user_id=99999)


# ---------------------------------------------------------------------------
# canonical_merge_queue_config — callable factory (NOT a fixture).
#
# Returns the canonical MergeQueueConfig for the six hipDNN-ecosystem queues
# per RFC §4.2. Both Plan 04 and Plan 05 call this function at module scope or
# from state-machine @initialize methods — it MUST be a plain callable (not a
# pytest fixture) so non-pytest code can import it.
#
# Rule: Plans 04 + 05 import from tests.conftest ONLY.
#   - Plan 04 _strategies.py does NOT redefine or re-export this symbol.
#   - Plan 05 worked-example constants module does NOT copy-paste this.
# Locking the single owning home eliminates two-copy drift (T-01-12b).
# ---------------------------------------------------------------------------

# RFC §4.2 path → queue mapping, pre-sorted longest-first.
# Ordering is the loader's responsibility; these fixtures already satisfy it.
_CANONICAL_PATH_TO_QUEUES: tuple[tuple[str, frozenset[str]], ...] = (
    # longest prefixes first
    (
        "dnn-providers/integration-tests/",
        frozenset(
            {
                "miopen-provider",
                "hipblaslt-provider",
                "composable-kernel-provider",
                "rocblas-provider",
                "integration-tests",
            }
        ),
    ),
    (
        "projects/hipdnn/",
        frozenset(
            {
                "hipdnn",
                "miopen-provider",
                "hipblaslt-provider",
                "composable-kernel-provider",
                "rocblas-provider",
                "integration-tests",
            }
        ),
    ),
    (
        "dnn-providers/miopen-provider/",
        frozenset({"miopen-provider"}),
    ),
    (
        "dnn-providers/hipblaslt-provider/",
        frozenset({"hipblaslt-provider"}),
    ),
    (
        "dnn-providers/composable-kernel-provider/",
        frozenset({"composable-kernel-provider"}),
    ),
    (
        "dnn-providers/rocblas-provider/",
        frozenset({"rocblas-provider"}),
    ),
)

_CANONICAL_ALL_QUEUES: tuple[str, ...] = (
    "hipdnn",
    "miopen-provider",
    "hipblaslt-provider",
    "composable-kernel-provider",
    "rocblas-provider",
    "integration-tests",
)


def canonical_merge_queue_config() -> MergeQueueConfig:
    """Return the canonical MergeQueueConfig for the six hipDNN-ecosystem queues.

    Populated per RFC §4.2:
    - app_identity = CANONICAL_APP (slug="rocm-mq", app_id=12345, bot_user_id=99999)
    - queues: hipdnn, miopen-provider, hipblaslt-provider, composable-kernel-provider,
      rocblas-provider, integration-tests
    - path_to_queues: pre-sorted longest-prefix-first (integration-tests/ before hipdnn/)
    - activation_status_context: "merge-queue/active"
    - active_label: "mq:active", queued_label: "mq:queued"

    This is the single owning home for this configuration (T-01-12b). Plan 04 invariant
    suite (RuleBasedStateMachine @initialize) and Plan 05 worked-example constants both
    import this callable from tests.conftest — NOT from tests._strategies.
    """
    return MergeQueueConfig(
        all_queues=_CANONICAL_ALL_QUEUES,
        path_to_queues=_CANONICAL_PATH_TO_QUEUES,
        app_identity=CANONICAL_APP,
        activation_status_context="merge-queue/active",
        active_label="mq:active",
        queued_label="mq:queued",
    )


# ---------------------------------------------------------------------------
# AppIdentity fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def canonical_app_identity() -> AppIdentity:
    """The canonical merge-queue App identity (sentinel values for tests)."""
    return CANONICAL_APP


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
# Per-state PR fixtures (Plan 03 renderer test scenarios)
#
# These are realistic instances matching the scenario descriptions in PLAN.md
# Task 1 action section.  Each pr_state__* is paired with a render_ctx__* fixture.
# ---------------------------------------------------------------------------


@pytest.fixture()
def pr_state__miopen_pr_queued_position_2_of_3() -> PRState:
    """PR #42 touching miopen-provider only, queued at position 2 of 3."""
    return PRState(
        number=42,
        head_sha="cafe0001cafe0002cafe0003cafe0004cafe0005",
        labels=frozenset({"mq:queued", "mq:miopen-provider"}),
        queues=frozenset({"miopen-provider"}),
        enqueued_at=utc(2026, 4, 22, 14, 10),
        is_validly_active=False,
    )


@pytest.fixture()
def render_ctx__miopen_pr_queued_position_2_of_3() -> RenderContext:
    """RenderContext for miopen PR queued at position 2 of 3."""
    return RenderContext(
        author_login="alice",
        pr_title="Fix MIOpen kernel autotuning",
        queue_positions=(("miopen-provider", 2, 3),),
        blockers=(41,),
        cycle_run_url="https://github.com/SamuelReeder/rocm-libraries/actions/runs/1234567",
        state="queued",
        eject_reason=None,
        merged_sha=None,
    )


@pytest.fixture()
def pr_state__core_pr_active_checks_pending() -> PRState:
    """PR #99 touching projects/hipdnn/ (all six queues), is_validly_active=True."""
    return PRState(
        number=99,
        head_sha="abc1234abc1234abc1234abc1234abc1234abc12",
        labels=frozenset(
            {
                "mq:queued",
                "mq:active",
                "mq:hipdnn",
                "mq:miopen-provider",
                "mq:hipblaslt-provider",
                "mq:composable-kernel-provider",
                "mq:rocblas-provider",
                "mq:integration-tests",
            }
        ),
        queues=frozenset(
            {
                "hipdnn",
                "miopen-provider",
                "hipblaslt-provider",
                "composable-kernel-provider",
                "rocblas-provider",
                "integration-tests",
            }
        ),
        enqueued_at=utc(2026, 4, 22, 9, 5),
        is_validly_active=True,
    )


@pytest.fixture()
def render_ctx__core_pr_active_checks_pending() -> RenderContext:
    """RenderContext for active PR with checks pending."""
    return RenderContext(
        author_login="bob",
        pr_title="[hipDNN] Add SDPA forward kernel",
        queue_positions=(
            ("hipdnn", 1, 1),
            ("miopen-provider", 1, 2),
            ("hipblaslt-provider", 1, 1),
            ("composable-kernel-provider", 1, 1),
            ("rocblas-provider", 1, 1),
            ("integration-tests", 1, 1),
        ),
        blockers=(),
        cycle_run_url="https://github.com/SamuelReeder/rocm-libraries/actions/runs/1234568",
        state="active",
        eject_reason=None,
        merged_sha=None,
    )


@pytest.fixture()
def pr_state__core_pr_merged() -> PRState:
    """PR #99 post-merge (labels cleared)."""
    return PRState(
        number=99,
        head_sha="abc1234abc1234abc1234abc1234abc1234abc12",
        labels=frozenset(),
        queues=frozenset(),
        enqueued_at=utc(2026, 4, 22, 9, 5),
        is_validly_active=False,
    )


@pytest.fixture()
def render_ctx__core_pr_merged() -> RenderContext:
    """RenderContext for merged PR."""
    return RenderContext(
        author_login="bob",
        pr_title="[hipDNN] Add SDPA forward kernel",
        queue_positions=(),
        blockers=(),
        cycle_run_url="https://github.com/SamuelReeder/rocm-libraries/actions/runs/1234568",
        state="merged",
        eject_reason=None,
        merged_sha="def5678def5678def5678def5678def5678def5",
    )


@pytest.fixture()
def pr_state__core_pr_ejected_ci_failure() -> PRState:
    """PR #99 ejected due to CI failure."""
    return PRState(
        number=99,
        head_sha="abc1234abc1234abc1234abc1234abc1234abc12",
        labels=frozenset({"mq:queued", "mq:active", "mq:hipdnn"}),
        queues=frozenset({"hipdnn"}),
        enqueued_at=utc(2026, 4, 22, 9, 5),
        is_validly_active=True,
    )


@pytest.fixture()
def render_ctx__core_pr_ejected_ci_failure() -> RenderContext:
    """RenderContext for CI-failure ejected PR."""
    return RenderContext(
        author_login="bob",
        pr_title="[hipDNN] Add SDPA forward kernel",
        queue_positions=(),
        blockers=(),
        cycle_run_url="https://github.com/SamuelReeder/rocm-libraries/actions/runs/1234570",
        state="ejected",
        eject_reason="TheRock CI Summary failed on commit abc1234",
        merged_sha=None,
    )


@pytest.fixture()
def pr_state__core_pr_ejected_approval_revoked() -> PRState:
    """PR #99 ejected because approval was revoked."""
    return PRState(
        number=99,
        head_sha="abc1234abc1234abc1234abc1234abc1234abc12",
        labels=frozenset({"mq:queued", "mq:hipdnn"}),
        queues=frozenset({"hipdnn"}),
        enqueued_at=utc(2026, 4, 22, 9, 5),
        is_validly_active=False,
    )


@pytest.fixture()
def render_ctx__core_pr_ejected_approval_revoked() -> RenderContext:
    """RenderContext for approval-revoked ejected PR."""
    return RenderContext(
        author_login="bob",
        pr_title="[hipDNN] Add SDPA forward kernel",
        queue_positions=(),
        blockers=(),
        cycle_run_url=None,
        state="ejected",
        eject_reason="approval revoked",
        merged_sha=None,
    )
