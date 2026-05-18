"""
test_helpers_is_app_identity.py — Unit + property tests for ``is_app_identity``.

PURE-05: ``is_app_identity`` checks ``type == "Bot"`` AND ``app_slug == expected.slug``
AND ``app_id == expected.app_id`` (triple-check, RFC §4.3.1 + Pitfall 2).

Tests:
1. Parametrized canonical + 5 non-canonical fixture variants (6 parametrized cases).
2. Per-check-failure-mode tests (slug wrong, ID wrong, type not Bot) — failure attribution.
3. Property test: any creator that differs from canonical in at least one checked field
   returns False.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rocm_mq._helpers import is_app_identity
from rocm_mq.state import AppIdentity, CommitStatusCreator

# ---------------------------------------------------------------------------
# Sentinel canonical identity (must match conftest.canonical_app_identity)
# ---------------------------------------------------------------------------

_CANONICAL = AppIdentity(slug="rocm-mq", app_id=12345, bot_user_id=99999)
_CANONICAL_CREATOR = CommitStatusCreator(
    login="rocm-mq[bot]",
    type="Bot",
    app_slug="rocm-mq",
    app_id=12345,
)


# ---------------------------------------------------------------------------
# Parametrized unit tests over all six variants
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("creator", "expected_result"),
    [
        # Canonical — the only True case
        (
            CommitStatusCreator(login="rocm-mq[bot]", type="Bot", app_slug="rocm-mq", app_id=12345),
            True,
        ),
        # Wrong slug
        (
            CommitStatusCreator(
                login="rocm-mq[bot]", type="Bot", app_slug="rocm-mp", app_id=12345
            ),
            False,
        ),
        # Wrong app_id
        (
            CommitStatusCreator(
                login="rocm-mq[bot]", type="Bot", app_slug="rocm-mq", app_id=54321
            ),
            False,
        ),
        # Right slug+ID but type=User (impersonator)
        (
            CommitStatusCreator(
                login="rocm-mq", type="User", app_slug="rocm-mq", app_id=12345
            ),
            False,
        ),
        # github-actions[bot] — sibling workflow using GITHUB_TOKEN
        (
            CommitStatusCreator(
                login="github-actions[bot]",
                type="Bot",
                app_slug="github-actions",
                app_id=15368,
            ),
            False,
        ),
        # Bare [bot] suffix, no app sub-object (app_slug=None, app_id=None)
        (
            CommitStatusCreator(
                login="rocm-mq[bot]", type="Bot", app_slug=None, app_id=None
            ),
            False,
        ),
    ],
    ids=[
        "canonical",
        "wrong_slug",
        "wrong_id",
        "user_type",
        "sibling_workflow",
        "bare_bot_no_app",
    ],
)
def test_is_app_identity_parametrized(
    creator: CommitStatusCreator,
    expected_result: bool,
) -> None:
    """Canonical variant returns True; all five non-canonical variants return False."""
    assert is_app_identity(creator, _CANONICAL) is expected_result


# ---------------------------------------------------------------------------
# Per-check-failure-mode tests (named for precise failure attribution)
# ---------------------------------------------------------------------------


def test_is_app_identity_false_when_slug_wrong() -> None:
    """Slug-only failure: right type+ID, wrong slug → False."""
    creator = CommitStatusCreator(
        login="rocm-mq[bot]", type="Bot", app_slug="rocm-mp", app_id=12345
    )
    assert is_app_identity(creator, _CANONICAL) is False


def test_is_app_identity_false_when_app_id_wrong() -> None:
    """App-ID-only failure: right type+slug, wrong ID → False."""
    creator = CommitStatusCreator(
        login="rocm-mq[bot]", type="Bot", app_slug="rocm-mq", app_id=54321
    )
    assert is_app_identity(creator, _CANONICAL) is False


def test_is_app_identity_false_when_type_not_bot() -> None:
    """Type failure: right slug+ID but type != 'Bot' → False."""
    creator = CommitStatusCreator(
        login="rocm-mq", type="User", app_slug="rocm-mq", app_id=12345
    )
    assert is_app_identity(creator, _CANONICAL) is False


def test_is_app_identity_false_when_both_slug_and_id_wrong() -> None:
    """Both slug and ID differ — False (covers the 'some-other-app' case)."""
    creator = CommitStatusCreator(
        login="other-bot[bot]", type="Bot", app_slug="other-app", app_id=99999
    )
    assert is_app_identity(creator, _CANONICAL) is False


def test_is_app_identity_true_for_canonical_via_fixtures(
    creator_canonical: CommitStatusCreator,
    canonical_app_identity: AppIdentity,
) -> None:
    """Canonical creator + canonical identity → True (uses pytest fixtures)."""
    assert is_app_identity(creator_canonical, canonical_app_identity) is True


def test_is_app_identity_false_for_all_non_canonical(
    non_canonical_creator: CommitStatusCreator,
    canonical_app_identity: AppIdentity,
) -> None:
    """Every non-canonical variant → False (parametrized via conftest fixture)."""
    assert is_app_identity(non_canonical_creator, canonical_app_identity) is False


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


@given(
    app_slug=st.one_of(
        st.just("rocm-mp"),  # wrong slug
        st.text(min_size=1, max_size=50).filter(lambda s: s != "rocm-mq"),
    ),
    app_id=st.integers(min_value=1),
)
def test_is_app_identity_false_when_slug_differs_property(
    app_slug: str,
    app_id: int,
) -> None:
    """Any creator with a slug != 'rocm-mq' returns False, regardless of other fields."""
    creator = CommitStatusCreator(
        login="rocm-mq[bot]",
        type="Bot",
        app_slug=app_slug,
        app_id=app_id,
    )
    assert is_app_identity(creator, _CANONICAL) is False


@given(
    app_id=st.integers(min_value=1).filter(lambda n: n != 12345),
)
def test_is_app_identity_false_when_app_id_differs_property(app_id: int) -> None:
    """Any creator with app_id != 12345 returns False, even with right slug."""
    creator = CommitStatusCreator(
        login="rocm-mq[bot]",
        type="Bot",
        app_slug="rocm-mq",
        app_id=app_id,
    )
    assert is_app_identity(creator, _CANONICAL) is False


@given(
    actor_type=st.text(min_size=1).filter(lambda t: t != "Bot"),
)
def test_is_app_identity_false_when_type_not_bot_property(actor_type: str) -> None:
    """Any creator with type != 'Bot' returns False, even with right slug+ID."""
    creator = CommitStatusCreator(
        login="rocm-mq[bot]",
        type=actor_type,
        app_slug="rocm-mq",
        app_id=12345,
    )
    assert is_app_identity(creator, _CANONICAL) is False


@given(
    login=st.text(min_size=0, max_size=100),
    actor_type=st.sampled_from(["Bot", "User", "Organization"]),
    app_slug=st.one_of(st.none(), st.text(min_size=0, max_size=50)),
    app_id=st.one_of(st.none(), st.integers(min_value=0)),
)
def test_is_app_identity_canonical_only_when_all_three_match(
    login: str,
    actor_type: str,
    app_slug: str | None,
    app_id: int | None,
) -> None:
    """is_app_identity returns True only when type=Bot AND slug=rocm-mq AND app_id=12345."""
    creator = CommitStatusCreator(
        login=login,
        type=actor_type,
        app_slug=app_slug,
        app_id=app_id,
    )
    result = is_app_identity(creator, _CANONICAL)
    expected = actor_type == "Bot" and app_slug == "rocm-mq" and app_id == 12345
    assert result is expected
