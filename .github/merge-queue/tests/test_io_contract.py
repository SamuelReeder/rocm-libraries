"""
tests/test_io_contract.py — IO-06 contract tests for the four load-bearing
GitHub semantic behaviors that the merge queue depends on.

Why a "contract" test (vs a plain unit test): the fake (``tests/gh_fake.py``)
is the substitute for the real GitHub API in every test outside this file.
If the fake silently drifts from real GitHub semantics, the entire executor
suite passes vacuously and a real-API bug surfaces only at processor-run
time (every 3 minutes — the worst possible feedback latency, RFC §4.6).

The four behaviors locked here:

  1. **(SHA, context) status overwrite** — second ``create_commit_status``
     on the same ``(sha, context)`` REPLACES the first. Status created/read
     on different contexts are independent.
  2. **Label add idempotency** — adding an existing label is a no-op (no
     duplicate, no error).
  3. **``remove_label`` returns 404 when absent** — the executor relies on
     catching this so unlabel is idempotent across restarts.
  4. **``repos.merge`` returns 204 when already up-to-date, 201 with a fresh
     SHA otherwise** — used by the post-squash branch fast-forward step.

Plus a bonus "creator identity distinction" pair that verifies the
SimpleUser bridging in ``snapshot._make_status_creator`` accepts the App's
own statuses and rejects sibling-workflow ones (RFC §4.3.1).

Real-API path: the parametrize list is hard-coded to ``["fake"]``; the
"real" path is reserved for an opt-in nightly CI job that sets
``GITHUB_TOKEN`` and exercises the same assertions against a live
sandbox repo. Adding the real path is a one-line change here and a
``GITHUB_TOKEN`` environment-scoped secret in the workflow.
"""

from __future__ import annotations

import os
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
# Parametrized fixture: "fake" always; "real" skipped without GITHUB_TOKEN.
# ---------------------------------------------------------------------------


@pytest.fixture(params=["fake"])
def github_client_and_state(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[Any, FakeRepoState]]:
    """Yield ``(client, state)`` for the parametrized backend."""
    if request.param == "fake":
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
        return
    if request.param == "real":  # pragma: no cover - reserved for nightly CI
        if not os.environ.get("GITHUB_TOKEN"):
            pytest.skip("real client requires GITHUB_TOKEN env var")
        # When enabled: construct a real GitHubClient against a sandbox repo,
        # yield it together with a state-shape object that mirrors what the
        # tests need. Left unimplemented until the nightly job exists.
        pytest.skip("real backend not yet wired (nightly CI path)")
    raise ValueError(f"unknown backend: {request.param!r}")


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
# Bonus: creator identity bridging — App vs sibling workflow
# (T-02-02-01 / T-02-02-05 mitigation verification)
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
