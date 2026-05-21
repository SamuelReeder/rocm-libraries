"""Contract tests for the load-bearing GitHub semantic behaviors that the
merge queue depends on.

Why a "contract" test (vs a plain unit test): the fake (``tests/gh_fake.py``)
is the substitute for the real GitHub API in every test outside this file.
If the fake silently drifts from real GitHub semantics, the entire executor
suite passes vacuously and a real-API bug surfaces only at processor-run
time (every 3 minutes — the worst possible feedback latency, RFC §4.6).

The behaviors locked here:

  1. **(SHA, context) status overwrite** — second ``create_commit_status``
     on the same ``(sha, context)`` REPLACES the first. Status created/read
     on different contexts are independent.
  2. **Label add idempotency** — adding an existing label is a no-op (no
     duplicate, no error).
  3. **``remove_label`` returns 404 when absent** — the executor relies on
     catching this so unlabel is idempotent across restarts.
  4. **``repos.merge`` returns 204 when already up-to-date, 201 with a fresh
     SHA otherwise** — used by the post-squash branch fast-forward step.
  5. **``compare_commits`` direction- and ancestry-awareness** — the fake
     parses ``basehead``, walks the seeded commits graph, and answers
     'ahead'/'behind'/'identical'/'diverged' to match real GitHub.

Plus a "creator identity distinction" pair that verifies the SimpleUser
bridging in ``snapshot._make_status_creator`` accepts the App's own statuses
and rejects sibling-workflow ones (RFC §4.3.1).

Backend: only the in-memory ``FakeGitHub`` is exercised here. A previous
revision of this file declared a parametrized ``"real"`` backend that was
unreachable (the params list was hard-coded to ``["fake"]``) and gated
behind a nonexistent nightly CI job. Documenting a "real" path that
never runs is worse than not documenting it — it gives a false sense of
fake-vs-real drift coverage. Removed entirely. To add real-API coverage
in the future, introduce a separate test module gated on
``PYTEST_RUN_REAL_GITHUB=1`` and a scheduled CI workflow that mints the
token via ``actions/create-github-app-token`` against a sandbox repo.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from githubkit.exception import RequestFailed

from rocm_mq._helpers import is_app_identity
from rocm_mq.snapshot import _make_status_creator
from tests.conftest import CANONICAL_APP, canonical_merge_queue_config
from tests.gh_fake import FakeGitHub, FakePR, FakeRepoState

OWNER = "SamuelReeder"
REPO = "rocm-libraries"


# ---------------------------------------------------------------------------
# Fake-backend fixture (dormant "real" backend branch removed).
# ---------------------------------------------------------------------------


@pytest.fixture
def github_client_and_state() -> Iterator[tuple[Any, FakeRepoState]]:
    """Yield ``(client, state)`` for the in-memory fake backend."""
    state = FakeRepoState(
        prs={
            1: FakePR(
                number=1,
                head_sha="head_sha_1",
                labels={"mq:queued", "mq:hipdnn"},
                files=["projects/hipdnn/foo.cpp"],
            ),
            2: FakePR(
                number=2,
                head_sha="head_sha_2",
                labels={"mq:queued"},
            ),
        },
        develop_tip="develop_initial_tip",
    )
    yield FakeGitHub(state, creator_type="app"), state


# ---------------------------------------------------------------------------
# Semantic 1: (SHA, context) status overwrite
# ---------------------------------------------------------------------------


def test_status_overwrite__second_write_replaces_first(
    github_client_and_state: tuple[Any, FakeRepoState],
) -> None:
    client, _state = github_client_and_state
    sha = "abc123"
    ctx = "merge-queue/active"

    client.rest.repos.create_commit_status(
        OWNER, REPO, sha, state="pending", context=ctx
    )
    client.rest.repos.create_commit_status(
        OWNER, REPO, sha, state="success", context=ctx
    )

    statuses = client.rest.repos.list_commit_statuses_for_ref(
        OWNER, REPO, sha
    ).parsed_data
    matches = [s for s in statuses if s.context == ctx]
    assert len(matches) == 1, "(sha, context) overwrite must yield a single entry"
    assert matches[0].state == "success", "second write must replace first"


def test_status_overwrite__different_context__both_visible(
    github_client_and_state: tuple[Any, FakeRepoState],
) -> None:
    client, _state = github_client_and_state
    sha = "def456"

    client.rest.repos.create_commit_status(
        OWNER, REPO, sha, state="pending", context="ci/build"
    )
    client.rest.repos.create_commit_status(
        OWNER, REPO, sha, state="success", context="ci/test"
    )

    statuses = client.rest.repos.list_commit_statuses_for_ref(
        OWNER, REPO, sha
    ).parsed_data
    contexts = {s.context for s in statuses}
    assert contexts == {"ci/build", "ci/test"}


# ---------------------------------------------------------------------------
# Semantic 2: label add idempotency
# ---------------------------------------------------------------------------


def test_add_label__already_present__no_duplicate(
    github_client_and_state: tuple[Any, FakeRepoState],
) -> None:
    client, state = github_client_and_state
    # PR #1 already has mq:queued from the fixture initial state.
    assert "mq:queued" in state.prs[1].labels

    client.rest.issues.add_labels(OWNER, REPO, 1, data={"labels": ["mq:queued"]})

    occurrences = sum(1 for ln in state.prs[1].labels if ln == "mq:queued")
    assert occurrences == 1, "label-add must be idempotent (set semantics)"


def test_add_label__new_label__appears(
    github_client_and_state: tuple[Any, FakeRepoState],
) -> None:
    client, state = github_client_and_state
    client.rest.issues.add_labels(OWNER, REPO, 2, data={"labels": ["mq:active"]})
    assert "mq:active" in state.prs[2].labels


# ---------------------------------------------------------------------------
# Semantic 3: remove_label 404 when absent
# ---------------------------------------------------------------------------


def test_remove_label__absent__raises_404(
    github_client_and_state: tuple[Any, FakeRepoState],
) -> None:
    client, _state = github_client_and_state
    with pytest.raises(RequestFailed) as excinfo:
        client.rest.issues.remove_label(OWNER, REPO, 1, "definitely-not-on-pr")
    assert excinfo.value.response.status_code == 404


def test_remove_label__present__removes(
    github_client_and_state: tuple[Any, FakeRepoState],
) -> None:
    client, state = github_client_and_state
    assert "mq:hipdnn" in state.prs[1].labels
    client.rest.issues.remove_label(OWNER, REPO, 1, "mq:hipdnn")
    assert "mq:hipdnn" not in state.prs[1].labels


# ---------------------------------------------------------------------------
# Semantic 4: repos.merge 204 vs 201
# ---------------------------------------------------------------------------


def test_repos_merge__already_up_to_date__returns_204(
    github_client_and_state: tuple[Any, FakeRepoState],
) -> None:
    client, state = github_client_and_state
    # Use the current develop_tip as head → fast-forward not needed.
    resp = client.rest.repos.merge(
        OWNER, REPO, base="develop", head=state.develop_tip
    )
    assert resp.status_code == 204
    assert resp.parsed_data is None


def test_repos_merge__new_commit__returns_201_with_sha(
    github_client_and_state: tuple[Any, FakeRepoState],
) -> None:
    client, state = github_client_and_state
    prior_tip = state.develop_tip
    resp = client.rest.repos.merge(
        OWNER, REPO, base="develop", head="some_other_sha"
    )
    assert resp.status_code == 201
    assert resp.parsed_data is not None
    assert isinstance(resp.parsed_data.sha, str) and resp.parsed_data.sha
    assert state.develop_tip != prior_tip, "develop tip must advance after merge"


# ---------------------------------------------------------------------------
# Semantic 5: compare_commits direction-of-comparison
# ---------------------------------------------------------------------------
# The fake's compare_commits MUST be input-aware: it parses ``basehead``,
# walks the seeded commits graph, and returns a verdict that reflects the
# actual ancestry relationship between the two SHAs. An input-agnostic stub
# would mask three classes of executor-side regression:
#   1. A future _verify_squash refactor that swaps direction to
#      f"{squash}...{develop}" (would change meaning to "is develop ahead
#      of squash"; real API returns 'behind'; the fake MUST too).
#   2. A typo passing one-dot or empty separator (real API returns 422).
#   3. A regression that calls compare with unrelated SHAs (real API
#      returns 'diverged').


def test_compare_commits__ahead_when_base_is_ancestor_of_head(
    github_client_and_state: tuple[Any, FakeRepoState],
) -> None:
    """basehead='base...head' where base is reachable by walking head's parents
    → status='ahead' with a non-empty files list (the happy path)."""
    client, state = github_client_and_state
    state.commits["head_sha_X"] = {"parents": ["base_sha_Y"], "message": "X"}
    resp = client.rest.repos.compare_commits(
        OWNER, REPO, basehead="base_sha_Y...head_sha_X"
    )
    assert resp.parsed_data.status == "ahead"
    assert len(resp.parsed_data.files) >= 1


def test_compare_commits__behind_when_direction_is_swapped(
    github_client_and_state: tuple[Any, FakeRepoState],
) -> None:
    """basehead='head...base' (wrong direction) → status='behind', files=[].

    Pins a regression that an input-agnostic stub could not catch: a future
    refactor that swaps the basehead direction would change the semantic
    from "did the squash advance develop" to "is develop ahead of squash".
    The real API answers 'behind' to the swapped form; the fake MUST too so
    that direction-swap regressions fail at test time.
    """
    client, state = github_client_and_state
    state.commits["head_sha_X"] = {"parents": ["base_sha_Y"], "message": "X"}
    resp = client.rest.repos.compare_commits(
        OWNER, REPO, basehead="head_sha_X...base_sha_Y"
    )
    assert resp.parsed_data.status == "behind"
    assert resp.parsed_data.files == []


def test_compare_commits__identical_when_base_equals_head(
    github_client_and_state: tuple[Any, FakeRepoState],
) -> None:
    """basehead='X...X' → status='identical', files=[]. Apr-2026 silent-corruption shape."""
    client, _state = github_client_and_state
    resp = client.rest.repos.compare_commits(
        OWNER, REPO, basehead="same_sha...same_sha"
    )
    assert resp.parsed_data.status == "identical"
    assert resp.parsed_data.files == []


def test_compare_commits__diverged_when_no_ancestry(
    github_client_and_state: tuple[Any, FakeRepoState],
) -> None:
    """basehead with two unrelated SHAs → status='diverged', files=[]."""
    client, _state = github_client_and_state
    # No commits seeded for either SHA → no ancestry relationship.
    resp = client.rest.repos.compare_commits(
        OWNER, REPO, basehead="lone_sha_A...lone_sha_B"
    )
    assert resp.parsed_data.status == "diverged"
    assert resp.parsed_data.files == []


def test_compare_commits__malformed_basehead__raises_422(
    github_client_and_state: tuple[Any, FakeRepoState],
) -> None:
    """basehead without '...' separator → RequestFailed(422), mirroring real API."""
    client, _state = github_client_and_state
    with pytest.raises(RequestFailed) as exc_info:
        client.rest.repos.compare_commits(OWNER, REPO, basehead="bad_basehead")
    assert exc_info.value.response.status_code == 422


def test_compare_commits__empty_basehead_half__raises_422(
    github_client_and_state: tuple[Any, FakeRepoState],
) -> None:
    """basehead with empty base or head half → RequestFailed(422)."""
    client, _state = github_client_and_state
    with pytest.raises(RequestFailed) as exc_info:
        client.rest.repos.compare_commits(OWNER, REPO, basehead="...just_head")
    assert exc_info.value.response.status_code == 422


# ---------------------------------------------------------------------------
# Bonus: creator identity bridging — App vs sibling workflow.
# Pins the activation-status creator filter: only the App's own statuses
# may be treated as authoritative; sibling github-actions[bot] is rejected.
# ---------------------------------------------------------------------------


def test_status_creator__app_creator__is_app_identity_true() -> None:
    """A status created by the App passes is_app_identity after bridging."""
    state = FakeRepoState()
    client = FakeGitHub(state, creator_type="app")
    config = canonical_merge_queue_config()

    client.rest.repos.create_commit_status(
        OWNER, REPO, "sha_a", state="success", context="merge-queue/active"
    )
    statuses = client.rest.repos.list_commit_statuses_for_ref(
        OWNER, REPO, "sha_a"
    ).parsed_data
    bridged = _make_status_creator(statuses[0].creator, config)
    assert is_app_identity(bridged, CANONICAL_APP) is True


def test_status_creator__workflow_creator__is_app_identity_false() -> None:
    """A status created by github-actions[bot] is rejected by is_app_identity."""
    state = FakeRepoState()
    client = FakeGitHub(state, creator_type="workflow")
    config = canonical_merge_queue_config()

    client.rest.repos.create_commit_status(
        OWNER, REPO, "sha_w", state="success", context="merge-queue/active"
    )
    statuses = client.rest.repos.list_commit_statuses_for_ref(
        OWNER, REPO, "sha_w"
    ).parsed_data
    bridged = _make_status_creator(statuses[0].creator, config)
    assert is_app_identity(bridged, CANONICAL_APP) is False
