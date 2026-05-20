"""
test_invariant_app_creator_filter_unspoofable.py — RFC §6 / D-02 / Q3 resolution:
App-creator filter is unspoofable.

Q3 resolution: This test uses adversarial Hypothesis fixtures (NOT RuleBasedStateMachine).
The invariant-suite machinery (shadow state, per-cycle snapshots) is irrelevant here
because the property is purely about derive_pr's is_validly_active computation on a
single RawPRState — no cycle sequencing is needed.

Five forged-creator variants (RESEARCH.md *Identity Helper* fixture matrix):
1. Wrong slug  (right type + right id, wrong slug)
2. Wrong app_id (right type + right slug, wrong id)
3. type=User   (impersonator account with same login name)
4. Sibling workflow (github-actions[bot] using GITHUB_TOKEN)
5. Bare bot, no app sub-object (app_slug=None, app_id=None)

Property: for every RawPRState whose head_statuses contain ONLY a forged
merge-queue/active status (right context, non-canonical creator), derive_pr
sets is_validly_active=False. The mq:active label is present — the forgery
attempt is structurally complete — but the creator filter still rejects it.

Uses @given (NOT RuleBasedStateMachine) because the invariant is a pure function
property of derive_pr, not a temporal/ordering property of the cycle loop.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings

from rocm_mq.decision import derive_pr
from rocm_mq.state import PRState
from tests._strategies import raw_pr_with_forged_activation_status_strategy
from tests.conftest import CANONICAL_APP, canonical_merge_queue_config, utc

# ---------------------------------------------------------------------------
# Module-level constants shared across tests
# ---------------------------------------------------------------------------

_CONFIG = canonical_merge_queue_config()
_NOW = utc(2026, 1, 1, 12, 0)


# ---------------------------------------------------------------------------
# Property-based test: forged activation status is always rejected
# ---------------------------------------------------------------------------


@given(raw_pr=raw_pr_with_forged_activation_status_strategy())
@settings(
    max_examples=500,
    suppress_health_check=[HealthCheck.too_slow],
    deadline=None,
)
def test_forged_activation_status_is_rejected(raw_pr) -> None:
    """A forged merge-queue/active status (right context, non-canonical creator)
    must never yield is_validly_active=True.

    The mq:active label is present on the raw PR — the adversary has done
    everything they can short of creating the status with the canonical App.
    derive_pr must still reject the activation via the creator filter (D-02).
    """
    result = derive_pr(raw_pr, _CONFIG, _NOW)
    # derive_pr returns PRState because mq:queued has a valid App-applied event;
    # the forged merge-queue/active status must NOT cause is_validly_active=True.
    assert isinstance(result, PRState), (
        f"Expected PRState (forged PR has valid enqueue event), "
        f"got {type(result).__name__!r} with reason={getattr(result, 'reason', None)!r}"
    )
    assert not result.is_validly_active, (
        f"APP-CREATOR-FILTER UNSPOOFABLE VIOLATED: "
        f"derive_pr returned is_validly_active=True for PR #{raw_pr.number} "
        f"even though the merge-queue/active status was created by a non-canonical "
        f"creator. Forged head_statuses: {raw_pr.head_statuses}"
    )


# ---------------------------------------------------------------------------
# Fixture-driven adversarial tests: one test per forged-creator variant
# These exhaustively pin the five specific attack vectors from RESEARCH.md.
# ---------------------------------------------------------------------------


def _make_forged_raw_pr_with_creator(creator):
    """Construct a minimal RawPRState with a forged merge-queue/active status
    using the given CommitStatusCreator.

    Includes one canonical App-applied mq:queued event so derive_pr succeeds
    to PRState (the forgery test is specifically about activation, not enqueue).
    """
    from rocm_mq.state import (
        CommitStatus,
        LabelEvent,
        RawPRState,
        TimelineActor,
    )

    app_actor = TimelineActor(
        login="rocm-mq[bot]",
        type="Bot",
        user_id=CANONICAL_APP.bot_user_id,
    )
    app_queued_event = LabelEvent(
        label_name="mq:queued",
        event="labeled",
        actor=app_actor,
        created_at=_NOW,
    )
    forged_status = CommitStatus(
        context=_CONFIG.activation_status_context,  # right context
        state="success",
        creator=creator,  # non-canonical creator
        created_at=_NOW,
    )
    return RawPRState(
        number=1,
        head_sha="a" * 40,
        labels=frozenset({"mq:queued", "mq:active", "mq:miopen-provider"}),
        head_statuses=(forged_status,),
        mq_queued_label_events=(app_queued_event,),
        changed_paths=(),
    )


def test_wrong_slug_creator_rejected(creator_wrong_slug) -> None:
    """Variant 1: right type + right id, wrong slug — creator filter must reject."""
    raw_pr = _make_forged_raw_pr_with_creator(creator_wrong_slug)
    result = derive_pr(raw_pr, _CONFIG, _NOW)
    assert isinstance(result, PRState)
    assert not result.is_validly_active, (
        f"Wrong-slug creator was not rejected: creator={creator_wrong_slug}"
    )


def test_wrong_app_id_creator_rejected(creator_wrong_id) -> None:
    """Variant 2: right type + right slug, wrong app_id — creator filter must reject."""
    raw_pr = _make_forged_raw_pr_with_creator(creator_wrong_id)
    result = derive_pr(raw_pr, _CONFIG, _NOW)
    assert isinstance(result, PRState)
    assert not result.is_validly_active, (
        f"Wrong-app_id creator was not rejected: creator={creator_wrong_id}"
    )


def test_user_type_creator_rejected(creator_user_type) -> None:
    """Variant 3: type=User (impersonator) — creator filter must reject."""
    raw_pr = _make_forged_raw_pr_with_creator(creator_user_type)
    result = derive_pr(raw_pr, _CONFIG, _NOW)
    assert isinstance(result, PRState)
    assert not result.is_validly_active, (
        f"User-type creator was not rejected: creator={creator_user_type}"
    )


def test_sibling_workflow_creator_rejected(creator_sibling_workflow) -> None:
    """Variant 4: github-actions[bot] (GITHUB_TOKEN sibling) — creator filter must reject."""
    raw_pr = _make_forged_raw_pr_with_creator(creator_sibling_workflow)
    result = derive_pr(raw_pr, _CONFIG, _NOW)
    assert isinstance(result, PRState)
    assert not result.is_validly_active, (
        f"Sibling-workflow creator was not rejected: creator={creator_sibling_workflow}"
    )


def test_bare_bot_no_app_creator_rejected(creator_bare_bot_no_app) -> None:
    """Variant 5: bare bot with no app sub-object (app_slug=None, app_id=None) — must reject."""
    raw_pr = _make_forged_raw_pr_with_creator(creator_bare_bot_no_app)
    result = derive_pr(raw_pr, _CONFIG, _NOW)
    assert isinstance(result, PRState)
    assert not result.is_validly_active, (
        f"Bare-bot-no-app creator was not rejected: creator={creator_bare_bot_no_app}"
    )


# ---------------------------------------------------------------------------
# Positive control: canonical creator IS accepted
# ---------------------------------------------------------------------------


def test_canonical_creator_is_accepted(creator_canonical) -> None:
    """Positive control: the canonical App creator must yield is_validly_active=True.

    This sanity-checks that the test is actually exercising the correct code path
    (the creator filter accepts when it should, not just always-False).
    """
    raw_pr = _make_forged_raw_pr_with_creator(creator_canonical)
    result = derive_pr(raw_pr, _CONFIG, _NOW)
    assert isinstance(result, PRState)
    assert result.is_validly_active, (
        f"Canonical creator was unexpectedly rejected: creator={creator_canonical}"
    )
