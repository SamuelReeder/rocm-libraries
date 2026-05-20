"""
tests/_strategies.py — Composable Hypothesis strategies for rocm_mq property tests.

These strategies generate only the fields the decision algorithm reads (D-03 tightness).
No st.text() noise on renderer-only fields like author_login or pr_title.

IMPORTANT: This module does NOT define canonical_merge_queue_config — that factory lives
in tests/conftest.py (Plan 03 single owning home). State-machine @initialize methods and
all invariant tests import from tests.conftest, NOT from here.

This module re-exports CANONICAL_APP from tests.conftest for convenience so strategy
consumers can reference the canonical App identity without a separate import.
"""

from __future__ import annotations

from datetime import UTC, datetime

from hypothesis import strategies as st

from rocm_mq.state import (
    AppIdentity,
    CommitStatus,
    CommitStatusCreator,
    LabelEvent,
    MergeQueueConfig,
    RawPRState,
    TimelineActor,
)

# Re-export from the single owning home (tests/conftest.py, Plan 03 territory).
# This re-export is allowed for convenience; tests._strategies.CANONICAL_APP resolves
# back to the same object defined in conftest.py — no second definition here.
from tests.conftest import CANONICAL_APP

# ---------------------------------------------------------------------------
# Sentinel constant — resolves from the conftest import above
# (re-exported here so strategy consumers have a single import point)
# ---------------------------------------------------------------------------

# CANONICAL_APP is imported above. It equals:
#   AppIdentity(slug="rocm-mq", app_id=12345, bot_user_id=99999)


# ---------------------------------------------------------------------------
# App identity strategies
# ---------------------------------------------------------------------------


def app_identity_strategy(*, canonical: bool = False) -> st.SearchStrategy[AppIdentity]:
    """Return an AppIdentity strategy.

    When canonical=True, returns st.just(CANONICAL_APP).
    When canonical=False, builds AppIdentity with slug/app_id filtered to NOT match.
    """
    if canonical:
        return st.just(CANONICAL_APP)
    return st.builds(
        AppIdentity,
        slug=st.text(min_size=1, max_size=20).filter(lambda s: s != CANONICAL_APP.slug),
        app_id=st.integers(min_value=1).filter(lambda i: i != CANONICAL_APP.app_id),
        bot_user_id=st.integers(min_value=1),
    )


# ---------------------------------------------------------------------------
# CommitStatusCreator strategies — canonical and five non-canonical variants
# ---------------------------------------------------------------------------

_CANONICAL_CREATOR = CommitStatusCreator(
    login="rocm-mq[bot]",
    type="Bot",
    app_slug=CANONICAL_APP.slug,
    app_id=CANONICAL_APP.app_id,
)


def commit_status_creator_strategy(
    *, canonical: bool = False
) -> st.SearchStrategy[CommitStatusCreator]:
    """Return a CommitStatusCreator strategy.

    When canonical=True: returns the canonical App creator.
    When canonical=False: returns one of the five non-canonical variants
    (wrong slug, wrong app_id, type=User, sibling github-actions[bot], bare bot no-app).
    These match the adversarial fixture matrix in RESEARCH.md lines 384-391.
    """
    if canonical:
        return st.just(_CANONICAL_CREATOR)

    return st.one_of(
        # Variant 1: Wrong slug (right type + right id, wrong slug)
        st.builds(
            CommitStatusCreator,
            login=st.just("rocm-mq[bot]"),
            type=st.just("Bot"),
            app_slug=st.text(min_size=1, max_size=20).filter(
                lambda s: s != CANONICAL_APP.slug
            ),
            app_id=st.just(CANONICAL_APP.app_id),
        ),
        # Variant 2: Wrong app_id (right type + right slug, wrong id)
        st.builds(
            CommitStatusCreator,
            login=st.just("rocm-mq[bot]"),
            type=st.just("Bot"),
            app_slug=st.just(CANONICAL_APP.slug),
            app_id=st.integers(min_value=1).filter(lambda i: i != CANONICAL_APP.app_id),
        ),
        # Variant 3: type=User (impersonator account named the same)
        st.builds(
            CommitStatusCreator,
            login=st.text(min_size=1, max_size=30),
            type=st.just("User"),
            app_slug=st.none(),
            app_id=st.none(),
        ),
        # Variant 4: github-actions[bot] (sibling workflow using GITHUB_TOKEN)
        st.just(
            CommitStatusCreator(
                login="github-actions[bot]",
                type="Bot",
                app_slug="github-actions",
                app_id=15368,  # GitHub Actions App ID
            )
        ),
        # Variant 5: bare [bot] suffix, no app sub-object
        st.just(
            CommitStatusCreator(
                login="rocm-mq[bot]",
                type="Bot",
                app_slug=None,
                app_id=None,
            )
        ),
    )


# ---------------------------------------------------------------------------
# Datetime strategy — always tz-aware (Pitfall 3)
# ---------------------------------------------------------------------------


def utc_datetime_strategy(
    min_year: int = 2025,
    max_year: int = 2027,
) -> st.SearchStrategy[datetime]:
    """Generate tz-aware datetimes in UTC.

    Always timezone-aware — prevents the Pitfall 3 naive-datetime FIFO
    corruption that would cause silent test-pass with wrong ordering.
    """
    return st.datetimes(
        min_value=datetime(min_year, 1, 1),
        max_value=datetime(max_year, 12, 31),
        timezones=st.just(UTC),
    )


# ---------------------------------------------------------------------------
# Queue subset strategy
# ---------------------------------------------------------------------------


def queue_subset_strategy(config: MergeQueueConfig) -> st.SearchStrategy[frozenset[str]]:
    """Generate a non-empty frozenset of queue names from config.all_queues."""
    return (
        st.sets(
            st.sampled_from(list(config.all_queues)),
            min_size=1,
            max_size=len(config.all_queues),
        )
        .map(frozenset)
    )


# ---------------------------------------------------------------------------
# LabelEvent strategy
# ---------------------------------------------------------------------------


def label_event_strategy(
    *,
    is_app: bool,
    label_name: str = "mq:queued",
) -> st.SearchStrategy[LabelEvent]:
    """Generate a LabelEvent with an appropriate actor.

    When is_app=True: actor is the canonical App bot (bot_user_id=99999).
    When is_app=False: actor is an arbitrary non-App user (user_id != bot_user_id).
    """
    if is_app:
        actor_strategy = st.just(
            TimelineActor(
                login="rocm-mq[bot]",
                type="Bot",
                user_id=CANONICAL_APP.bot_user_id,
            )
        )
    else:
        actor_strategy = st.builds(
            TimelineActor,
            login=st.text(min_size=1, max_size=30),
            type=st.sampled_from(["User", "Bot", "Organization"]),
            user_id=st.integers(min_value=1).filter(
                lambda i: i != CANONICAL_APP.bot_user_id
            ),
        )

    return st.builds(
        LabelEvent,
        label_name=st.just(label_name),
        event=st.just("labeled"),
        actor=actor_strategy,
        created_at=utc_datetime_strategy(),
    )


# (RequiredCheckResult strategy removed per 03-wr-09 — required-check
# evaluation is delegated to GitHub branch protection.)


# ---------------------------------------------------------------------------
# CommitStatus strategy — for head_statuses field
# ---------------------------------------------------------------------------


def _commit_status_strategy(
    *,
    canonical_creator: bool = False,
    activation_context: bool = False,
) -> st.SearchStrategy[CommitStatus]:
    """Build a CommitStatus.

    When canonical_creator=True AND activation_context=True: produces a valid
    merge-queue/active status that will pass is_validly_active.
    All other combinations produce non-activating statuses.
    """
    ctx = st.just("merge-queue/active") if activation_context else st.sampled_from([
        "ci/lint", "ci/build", "ci/test", "therock/build",
    ])
    return st.builds(
        CommitStatus,
        context=ctx,
        state=st.sampled_from(["pending", "success", "failure", "error"]),
        creator=commit_status_creator_strategy(canonical=canonical_creator),
        created_at=utc_datetime_strategy(),
    )


# ---------------------------------------------------------------------------
# RawPRState strategy — tight (only fields decide_cycle reads)
# ---------------------------------------------------------------------------


def raw_pr_strategy() -> st.SearchStrategy[RawPRState]:
    """Generate a tight RawPRState (only fields the algorithm reads — D-03).

    No noise generators for renderer-only fields (author_login, pr_title, etc.
    are not on RawPRState at all — they live on RenderContext).

    Generates several interesting combinations:
    - ~50% chance of App-applied mq:queued event (so derive_pr succeeds)
    - ~30% chance of right-context+canonical-creator merge-queue/active status
      (so some PRs are validly active for the no-squash-without-activation invariant)
    """
    from tests.conftest import canonical_merge_queue_config

    config = canonical_merge_queue_config()

    @st.composite
    def _make_raw_pr(draw: st.DrawFn) -> RawPRState:
        number = draw(st.integers(min_value=1, max_value=9999))
        head_sha = draw(
            st.text(
                alphabet="0123456789abcdef",
                min_size=40,
                max_size=40,
            )
        )

        # Build queue membership via mq:<name> labels
        member_queues = draw(queue_subset_strategy(config))
        base_labels: set[str] = {f"mq:{q}" for q in member_queues}

        # App-applied mq:queued event (~75% chance) — if absent, derive_pr returns
        # DeferredPR which is valid but means no PRState for decide_cycle
        has_app_queued_event = draw(st.booleans())
        app_queued_events: list[LabelEvent] = []
        if has_app_queued_event:
            # May have 1 or 2 events (re-enqueue scenario for FIFO invariant)
            n_events = draw(st.integers(min_value=1, max_value=2))
            for _ in range(n_events):
                app_queued_events.append(draw(label_event_strategy(is_app=True)))
            base_labels.add("mq:queued")

        # ~30% chance of a canonical-creator merge-queue/active status
        # (puts PR on the 5b path → potentially Squash)
        has_valid_activation = draw(st.booleans()) and has_app_queued_event
        head_statuses: list[CommitStatus] = []
        if has_valid_activation:
            base_labels.add("mq:active")
            head_statuses.append(
                draw(
                    st.builds(
                        CommitStatus,
                        context=st.just("merge-queue/active"),
                        state=st.just("success"),
                        creator=st.just(_CANONICAL_CREATOR),
                        created_at=utc_datetime_strategy(),
                    )
                )
            )
        # Add some other non-activating statuses
        extra_statuses = draw(st.lists(_commit_status_strategy(), min_size=0, max_size=2))
        head_statuses.extend(extra_statuses)

        return RawPRState(
            number=number,
            head_sha=head_sha,
            labels=frozenset(base_labels),
            head_statuses=tuple(head_statuses),
            mq_queued_label_events=tuple(app_queued_events),
            changed_paths=(),  # not read by decide_cycle
        )

    return _make_raw_pr()


# ---------------------------------------------------------------------------
# Adversarial RawPRState strategy — forged activation status
# ---------------------------------------------------------------------------


def raw_pr_with_forged_activation_status_strategy() -> st.SearchStrategy[RawPRState]:
    """Generate a RawPRState with a FORGED merge-queue/active status.

    The head_statuses tuple contains exactly one merge-queue/active status
    whose creator is NOT the canonical App (drawn from the five non-canonical
    variants). Used by test_invariant_app_creator_filter_unspoofable.py.

    Also includes one canonical-App-applied mq:queued event so derive_pr
    returns a PRState (not DeferredPR) — the test is specifically about
    activation status forgery, not enqueue tampering.
    """
    from tests.conftest import canonical_merge_queue_config

    config = canonical_merge_queue_config()

    @st.composite
    def _make_forged_raw_pr(draw: st.DrawFn) -> RawPRState:
        number = draw(st.integers(min_value=1, max_value=9999))
        head_sha = draw(
            st.text(
                alphabet="0123456789abcdef",
                min_size=40,
                max_size=40,
            )
        )

        # Pick a queue membership
        member_queues = draw(queue_subset_strategy(config))
        base_labels: set[str] = {f"mq:{q}" for q in member_queues}
        base_labels.add("mq:queued")
        base_labels.add("mq:active")  # label is present, but status is forged

        # One canonical App-applied mq:queued event (so derive_pr succeeds)
        app_queued_event = draw(label_event_strategy(is_app=True))

        # Forged activation status: right context, NON-canonical creator
        forged_creator = draw(commit_status_creator_strategy(canonical=False))
        forged_status = CommitStatus(
            context="merge-queue/active",
            state="success",
            creator=forged_creator,
            created_at=draw(utc_datetime_strategy()),
        )

        return RawPRState(
            number=number,
            head_sha=head_sha,
            labels=frozenset(base_labels),
            head_statuses=(forged_status,),
            mq_queued_label_events=(app_queued_event,),
            changed_paths=(),
        )

    return _make_forged_raw_pr()
