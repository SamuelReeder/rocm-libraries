"""
tests/test_derive.py — Unit tests for derive_pr (three-case logic) and
derive_snapshot, plus the is_validly_active adversarial matrix.

Coverage (per PLAN.md Task 2):
- Case 1 (normal): App-applied mq:queued event → PRState
- Case 2 (timeline lag): label present but no event → DeferredPR
- Case 3 (tampered): non-App-applied event only → DeferredPR
- is_validly_active matrix: 7 parametrized variants
- Property: adversarial creators yield is_validly_active=False
- derive_snapshot: collects PRState + emits Defer for DeferredPR
"""

from __future__ import annotations

from datetime import UTC

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rocm_mq.decision import derive_pr, derive_snapshot
from rocm_mq.state import (
    AppIdentity,
    CommitStatus,
    CommitStatusCreator,
    Defer,
    DeferredPR,
    LabelEvent,
    MergeQueueConfig,
    PartialPRState,
    PRState,
    RawPRState,
    RawSnapshot,
    RequiredCheckResult,
    Snapshot,
    TimelineActor,
)

# ---------------------------------------------------------------------------
# Helpers / constants
# ---------------------------------------------------------------------------

_APP = AppIdentity(slug="rocm-mq", app_id=12345, bot_user_id=99999)

_CONFIG = MergeQueueConfig(
    all_queues=(
        "hipdnn",
        "miopen-provider",
        "hipblaslt-provider",
        "hip-kernel-provider",
        "fusilli-provider",
        "integration-tests",
    ),
    path_to_queues=(),  # derive_pr does not call queues_for_paths
    app_identity=_APP,
)


def _utc(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> object:
    """Small tz-aware datetime helper (mirrors conftest.utc)."""
    from datetime import datetime

    return datetime(year, month, day, hour, minute, tzinfo=UTC)


def _app_actor() -> TimelineActor:
    """The canonical App's bot-user actor for label events."""
    return TimelineActor(login="rocm-mq[bot]", type="Bot", user_id=99999)


def _other_actor() -> TimelineActor:
    """A non-App user actor."""
    return TimelineActor(login="dev-user", type="User", user_id=42)


def _queued_event(
    actor: TimelineActor,
    created_at: object,
    config: MergeQueueConfig = _CONFIG,
) -> LabelEvent:
    from datetime import datetime

    assert isinstance(created_at, datetime)
    return LabelEvent(
        label_name=config.queued_label,
        event="labeled",
        actor=actor,
        created_at=created_at,
    )


def _canonical_status(config: MergeQueueConfig = _CONFIG) -> CommitStatus:
    """A valid merge-queue/active commit status from the canonical App."""
    from datetime import datetime

    creator = CommitStatusCreator(
        login="rocm-mq[bot]",
        type="Bot",
        app_slug="rocm-mq",
        app_id=12345,
    )
    return CommitStatus(
        context=config.activation_status_context,
        state="success",
        creator=creator,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# Case 1: Normal — App-applied mq:queued event present
# ---------------------------------------------------------------------------


def test_derive_pr__case1_normal_returns_pr_state() -> None:
    """Case 1: App-applied mq:queued event → returns PRState with correct fields."""
    from datetime import datetime

    enqueue_time = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    raw = RawPRState(
        number=42,
        head_sha="abc1234",
        labels=frozenset({"mq:queued", "mq:hipdnn"}),
        head_statuses=(),
        mq_queued_label_events=(
            _queued_event(_app_actor(), enqueue_time),
        ),
        required_check_results=(RequiredCheckResult(name="CI / build", state="pending"),),
        changed_paths=(),
    )
    result = derive_pr(raw, _CONFIG, enqueue_time)
    assert isinstance(result, PRState)
    assert result.number == 42
    assert result.head_sha == "abc1234"
    assert result.enqueued_at == enqueue_time
    # queues from mq: labels minus state labels ("mq:queued", "mq:active")
    assert result.queues == frozenset({"hipdnn"})
    assert result.is_validly_active is False  # no head status


def test_derive_pr__case1_enqueued_at_uses_most_recent_app_event() -> None:
    """Case 1: enqueued_at = max(app events) — re-enqueue moves to back of queue."""
    from datetime import datetime

    t1 = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
    t2 = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    raw = RawPRState(
        number=10,
        head_sha="sha1",
        labels=frozenset({"mq:queued", "mq:hipdnn"}),
        head_statuses=(),
        mq_queued_label_events=(
            _queued_event(_app_actor(), t1),
            _queued_event(_app_actor(), t2),  # later event = re-enqueue
        ),
        required_check_results=(),
        changed_paths=(),
    )
    result = derive_pr(raw, _CONFIG, t2)
    assert isinstance(result, PRState)
    assert result.enqueued_at == t2  # max wins


def test_derive_pr__case1_queues_excludes_state_labels() -> None:
    """Case 1: queues strips mq:queued and mq:active from label set."""
    from datetime import datetime

    t = datetime(2026, 1, 1, tzinfo=UTC)
    raw = RawPRState(
        number=11,
        head_sha="sha2",
        labels=frozenset({"mq:queued", "mq:active", "mq:hipdnn", "mq:miopen-provider"}),
        head_statuses=(),
        mq_queued_label_events=(_queued_event(_app_actor(), t),),
        required_check_results=(),
        changed_paths=(),
    )
    result = derive_pr(raw, _CONFIG, t)
    assert isinstance(result, PRState)
    assert result.queues == frozenset({"hipdnn", "miopen-provider"})


# ---------------------------------------------------------------------------
# Case 2: Timeline lag — label present but no App-applied event
# ---------------------------------------------------------------------------


def test_derive_pr__case2_label_without_event_returns_deferred() -> None:
    """Case 2: mq:queued label present but mq_queued_label_events=() → DeferredPR."""
    from datetime import datetime

    t = datetime(2026, 1, 1, tzinfo=UTC)
    raw = RawPRState(
        number=20,
        head_sha="sha3",
        labels=frozenset({"mq:queued"}),
        head_statuses=(),
        mq_queued_label_events=(),  # no events yet — timeline lag
        required_check_results=(),
        changed_paths=(),
    )
    result = derive_pr(raw, _CONFIG, t)
    assert isinstance(result, DeferredPR)
    assert result.number == 20
    assert "timeline event not yet visible" in result.reason


def test_derive_pr__case2_deferred_reason_is_descriptive() -> None:
    """Case 2: DeferredPR reason string gives enough info for debugging."""
    from datetime import datetime

    t = datetime(2026, 1, 1, tzinfo=UTC)
    raw = RawPRState(
        number=21,
        head_sha="sha4",
        labels=frozenset({"mq:queued", "mq:hipdnn"}),
        head_statuses=(),
        mq_queued_label_events=(),
        required_check_results=(),
        changed_paths=(),
    )
    result = derive_pr(raw, _CONFIG, t)
    assert isinstance(result, DeferredPR)
    # Must contain enough context that an operator reading logs knows what happened
    assert len(result.reason) > 10


# ---------------------------------------------------------------------------
# Case 3: Tampered — only non-App-applied events
# ---------------------------------------------------------------------------


def test_derive_pr__case3_non_app_event_only_returns_deferred() -> None:
    """Case 3: Only non-App mq:queued events → DeferredPR with 'non-App actor' reason."""
    from datetime import datetime

    t = datetime(2026, 1, 1, tzinfo=UTC)
    raw = RawPRState(
        number=30,
        head_sha="sha5",
        labels=frozenset({"mq:queued"}),
        head_statuses=(),
        mq_queued_label_events=(
            _queued_event(_other_actor(), t),  # non-App applied it
        ),
        required_check_results=(),
        changed_paths=(),
    )
    result = derive_pr(raw, _CONFIG, t)
    assert isinstance(result, DeferredPR)
    assert result.number == 30
    assert "non-App actor" in result.reason


def test_derive_pr__case3_non_app_event_even_with_label_is_deferred() -> None:
    """Case 3 precedence: non-App event is caught even when the label is present."""
    from datetime import datetime

    t = datetime(2026, 1, 1, tzinfo=UTC)
    raw = RawPRState(
        number=31,
        head_sha="sha6",
        labels=frozenset({"mq:queued", "mq:hipdnn"}),
        head_statuses=(),
        mq_queued_label_events=(
            _queued_event(_other_actor(), t),
        ),
        required_check_results=(),
        changed_paths=(),
    )
    result = derive_pr(raw, _CONFIG, t)
    assert isinstance(result, DeferredPR)


def test_derive_pr__no_enqueue_evidence_returns_deferred() -> None:
    """Case 'no evidence': no label AND no events → DeferredPR."""
    from datetime import datetime

    t = datetime(2026, 1, 1, tzinfo=UTC)
    raw = RawPRState(
        number=32,
        head_sha="sha7",
        labels=frozenset(),  # no mq:queued label
        head_statuses=(),
        mq_queued_label_events=(),
        required_check_results=(),
        changed_paths=(),
    )
    result = derive_pr(raw, _CONFIG, t)
    assert isinstance(result, DeferredPR)
    assert "no enqueue evidence" in result.reason


# ---------------------------------------------------------------------------
# is_validly_active matrix (parametrized)
# ---------------------------------------------------------------------------

# (description, status_creator_params, context_matches, expected)
_VALIDITY_CASES: list[
    tuple[str, dict[str, object], bool, bool]
] = [
    (
        "canonical_creator_right_context",
        {"login": "rocm-mq[bot]", "type": "Bot", "app_slug": "rocm-mq", "app_id": 12345},
        True,
        True,
    ),
    (
        "wrong_slug_right_context",
        {"login": "rocm-mq[bot]", "type": "Bot", "app_slug": "rocm-mp", "app_id": 12345},
        True,
        False,
    ),
    (
        "wrong_id_right_context",
        {"login": "rocm-mq[bot]", "type": "Bot", "app_slug": "rocm-mq", "app_id": 54321},
        True,
        False,
    ),
    (
        "user_type_right_context",
        {"login": "rocm-mq", "type": "User", "app_slug": "rocm-mq", "app_id": 12345},
        True,
        False,
    ),
    (
        "right_creator_wrong_context",
        {"login": "rocm-mq[bot]", "type": "Bot", "app_slug": "rocm-mq", "app_id": 12345},
        False,
        False,
    ),
    (
        "no_head_statuses",
        {},  # no status at all
        False,
        False,
    ),
]


@pytest.mark.parametrize(
    "case_name, creator_kwargs, context_matches, expected",
    [
        (c[0], c[1], c[2], c[3])
        for c in _VALIDITY_CASES
        if c[1]  # skip "no status" case — handle separately
    ],
    ids=[c[0] for c in _VALIDITY_CASES if c[1]],
)
def test_derive_pr__is_validly_active_matrix(
    case_name: str,
    creator_kwargs: dict[str, object],
    context_matches: bool,
    expected: bool,
) -> None:
    """is_validly_active matrix: context filter AND creator filter both required."""
    from datetime import datetime

    t = datetime(2026, 1, 1, tzinfo=UTC)
    creator = CommitStatusCreator(
        login=str(creator_kwargs["login"]),
        type=str(creator_kwargs["type"]),
        app_slug=creator_kwargs.get("app_slug"),  # type: ignore[arg-type]
        app_id=creator_kwargs.get("app_id"),  # type: ignore[arg-type]
    )
    context = (
        _CONFIG.activation_status_context if context_matches else "some-other-context"
    )
    status = CommitStatus(
        context=context,
        state="success",
        creator=creator,
        created_at=t,
    )
    raw = RawPRState(
        number=50,
        head_sha="sha8",
        labels=frozenset({"mq:queued"}),
        head_statuses=(status,),
        mq_queued_label_events=(_queued_event(_app_actor(), t),),
        required_check_results=(),
        changed_paths=(),
    )
    result = derive_pr(raw, _CONFIG, t)
    assert isinstance(result, PRState)
    assert result.is_validly_active is expected, (
        f"Case '{case_name}': expected is_validly_active={expected}"
    )


def test_derive_pr__no_head_statuses_is_not_validly_active() -> None:
    """No head_statuses → is_validly_active=False."""
    from datetime import datetime

    t = datetime(2026, 1, 1, tzinfo=UTC)
    raw = RawPRState(
        number=51,
        head_sha="sha9",
        labels=frozenset({"mq:queued"}),
        head_statuses=(),  # no statuses at all
        mq_queued_label_events=(_queued_event(_app_actor(), t),),
        required_check_results=(),
        changed_paths=(),
    )
    result = derive_pr(raw, _CONFIG, t)
    assert isinstance(result, PRState)
    assert result.is_validly_active is False


def test_derive_pr__multiple_statuses_one_valid_is_validly_active() -> None:
    """Multiple statuses where only one is valid → is_validly_active=True."""
    from datetime import datetime

    t = datetime(2026, 1, 1, tzinfo=UTC)
    bad_creator = CommitStatusCreator(
        login="github-actions[bot]",
        type="Bot",
        app_slug="github-actions",
        app_id=15368,
    )
    bad_status = CommitStatus(
        context=_CONFIG.activation_status_context,
        state="success",
        creator=bad_creator,
        created_at=t,
    )
    raw = RawPRState(
        number=52,
        head_sha="sha10",
        labels=frozenset({"mq:queued"}),
        head_statuses=(bad_status, _canonical_status()),  # one bad, one good
        mq_queued_label_events=(_queued_event(_app_actor(), t),),
        required_check_results=(),
        changed_paths=(),
    )
    result = derive_pr(raw, _CONFIG, t)
    assert isinstance(result, PRState)
    assert result.is_validly_active is True


# ---------------------------------------------------------------------------
# Property: adversarial creator → is_validly_active = False
# ---------------------------------------------------------------------------


def _non_canonical_creator_strategy() -> st.SearchStrategy[CommitStatusCreator]:
    """Strategy for non-canonical CommitStatusCreator instances."""
    return st.one_of(
        # Wrong slug
        st.builds(
            CommitStatusCreator,
            login=st.just("rocm-mq[bot]"),
            type=st.just("Bot"),
            app_slug=st.text().filter(lambda s: s != "rocm-mq"),
            app_id=st.just(12345),
        ),
        # Wrong app_id
        st.builds(
            CommitStatusCreator,
            login=st.just("rocm-mq[bot]"),
            type=st.just("Bot"),
            app_slug=st.just("rocm-mq"),
            app_id=st.integers().filter(lambda i: i != 12345),
        ),
        # User type
        st.builds(
            CommitStatusCreator,
            login=st.text(min_size=1),
            type=st.just("User"),
            app_slug=st.just("rocm-mq"),
            app_id=st.just(12345),
        ),
        # Sibling workflow (github-actions bot)
        st.just(
            CommitStatusCreator(
                login="github-actions[bot]",
                type="Bot",
                app_slug="github-actions",
                app_id=15368,
            )
        ),
        # Bot but no app object
        st.builds(
            CommitStatusCreator,
            login=st.text(min_size=1),
            type=st.just("Bot"),
            app_slug=st.none(),
            app_id=st.none(),
        ),
    )


@given(_non_canonical_creator_strategy())
@settings(max_examples=200)
def test_property__adversarial_creator_yields_not_validly_active(
    bad_creator: CommitStatusCreator,
) -> None:
    """For any non-canonical creator on merge-queue/active status, is_validly_active=False."""
    from datetime import datetime

    t = datetime(2026, 1, 1, tzinfo=UTC)
    status = CommitStatus(
        context=_CONFIG.activation_status_context,  # right context, wrong creator
        state="success",
        creator=bad_creator,
        created_at=t,
    )
    raw = RawPRState(
        number=60,
        head_sha="sha11",
        labels=frozenset({"mq:queued"}),
        head_statuses=(status,),
        mq_queued_label_events=(_queued_event(_app_actor(), t),),
        required_check_results=(),
        changed_paths=(),
    )
    result = derive_pr(raw, _CONFIG, t)
    assert isinstance(result, PRState)
    assert result.is_validly_active is False


# ---------------------------------------------------------------------------
# derive_snapshot tests
# ---------------------------------------------------------------------------


def test_derive_snapshot__normal_prs_go_into_snapshot() -> None:
    """derive_snapshot: fully derivable PRs appear in Snapshot.prs."""
    from datetime import datetime

    t = datetime(2026, 1, 1, tzinfo=UTC)
    app_actor = _app_actor()
    raw_snap = RawSnapshot(
        prs=(
            RawPRState(
                number=1,
                head_sha="s1",
                labels=frozenset({"mq:queued", "mq:hipdnn"}),
                head_statuses=(),
                mq_queued_label_events=(_queued_event(app_actor, t),),
                required_check_results=(),
                changed_paths=(),
            ),
            RawPRState(
                number=2,
                head_sha="s2",
                labels=frozenset({"mq:queued", "mq:miopen-provider"}),
                head_statuses=(),
                mq_queued_label_events=(_queued_event(app_actor, t),),
                required_check_results=(),
                changed_paths=(),
            ),
        )
    )
    snapshot, defers = derive_snapshot(raw_snap, _CONFIG, t)
    assert isinstance(snapshot, Snapshot)
    assert len(snapshot.prs) == 2
    assert len(defers) == 0
    assert {pr.number for pr in snapshot.prs} == {1, 2}


def test_derive_snapshot__deferred_prs_emit_defer_actions() -> None:
    """derive_snapshot: un-derivable PRs emit Defer actions with PartialPRState."""
    from datetime import datetime

    t = datetime(2026, 1, 1, tzinfo=UTC)
    raw_snap = RawSnapshot(
        prs=(
            # Normal PR
            RawPRState(
                number=1,
                head_sha="s1",
                labels=frozenset({"mq:queued"}),
                head_statuses=(),
                mq_queued_label_events=(_queued_event(_app_actor(), t),),
                required_check_results=(),
                changed_paths=(),
            ),
            # Timeline-lag PR (deferred)
            RawPRState(
                number=2,
                head_sha="s2",
                labels=frozenset({"mq:queued"}),
                head_statuses=(),
                mq_queued_label_events=(),  # no event yet
                required_check_results=(),
                changed_paths=(),
            ),
        )
    )
    snapshot, defers = derive_snapshot(raw_snap, _CONFIG, t)
    assert len(snapshot.prs) == 1
    assert snapshot.prs[0].number == 1
    assert len(defers) == 1
    defer = defers[0]
    assert isinstance(defer, Defer)
    assert isinstance(defer.pr, PartialPRState)
    assert defer.pr.number == 2


def test_derive_snapshot__returns_correct_types() -> None:
    """derive_snapshot returns (Snapshot, tuple[Defer, ...])."""
    from datetime import datetime

    t = datetime(2026, 1, 1, tzinfo=UTC)
    raw_snap = RawSnapshot(prs=())
    snapshot, defers = derive_snapshot(raw_snap, _CONFIG, t)
    assert isinstance(snapshot, Snapshot)
    assert isinstance(defers, tuple)
    assert len(snapshot.prs) == 0
    assert len(defers) == 0
