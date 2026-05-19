"""
tests/test_executor.py — Unit tests for rocm_mq.executor (IO-03, IO-04, IO-05).

Covers:
- ``dispatch(action, client, config, owner, repo)`` — exhaustive match over the
  five Action variants. Each variant routes to its private ``_handle_*`` helper
  (or, for Defer, returns success without any API call).
- ``_handle_activate`` — RFC §4.9 activation state machine:
  develop→PR merge → 201 (new merge commit) / 204 (already up-to-date) / 409
  (conflict, return failure) → pre-stamp race check (re-read PR head SHA;
  abort if changed) → stamp ``merge-queue/active`` commit status → label flip
  (add ``mq:active``, safely remove ``mq:queued``).
- Per-handler idempotency (RFC §4.6 stateless-processor contract; UK-3
  second-call scenarios): repos.merge 204, status overwrite, add_labels dedup,
  remove_label 404 swallowed, pulls.merge 405 swallowed.
- ``_verify_squash`` — IO-05 / Pitfall 8 defence (April 2026 silent corruption
  incident). Asserts the squash commit's ``parents[0].sha`` equals the
  ``develop`` branch tip recorded *before* the squash; retries 3x on 404 to
  absorb read-replication lag (RESEARCH.md UK-4).
- ``_find_status_comment_id`` — lazy ``<!-- rocm-mq-status -->`` marker
  discovery on PR issue comments.

Test patterns:
- ``FakeGitHub`` (tests.gh_fake) drives idempotency tests because it models the
  load-bearing semantics — (sha, context) overwrite, label dedup, 404 on absent
  label removal, 204 vs 201 on ``repos.merge``.
- ``unittest.mock.MagicMock`` drives the dispatch-table-row tests where we only
  need to verify the routing pattern.
- ``CANONICAL_APP`` and ``canonical_merge_queue_config()`` come from
  ``tests.conftest`` (single owning home).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from githubkit.exception import RequestFailed

from rocm_mq.gh import CorruptSquashError
from rocm_mq.state import (
    ActionOutcome,
    Activate,
    Defer,
    Eject,
    PartialPRState,
    PRState,
    Squash,
    UpdateComment,
)
from tests.conftest import canonical_merge_queue_config, utc
from tests.gh_fake import FakeGitHub, FakePR, FakeRepoState, _make_request_failed

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pr_state(
    *,
    number: int = 42,
    head_sha: str = "head_sha_aaa",
    labels: frozenset[str] = frozenset({"mq:queued", "mq:miopen-provider"}),
    queues: frozenset[str] = frozenset({"miopen-provider"}),
    is_validly_active: bool = False,
) -> PRState:
    """Build a PRState for executor tests (no behaviour from required_checks)."""
    return PRState(
        number=number,
        head_sha=head_sha,
        labels=labels,
        queues=queues,
        enqueued_at=utc(2026, 4, 22, 14, 10),
        is_validly_active=is_validly_active,
        required_check_results=(),
    )


def _make_fake_pulls_get_response(
    head_sha: str, head_ref: str = "feature-branch"
) -> SimpleNamespace:
    """Build a pulls.get response with both head.sha and head.ref populated."""
    return SimpleNamespace(
        parsed_data=SimpleNamespace(
            head=SimpleNamespace(sha=head_sha, ref=head_ref),
            base=SimpleNamespace(ref="develop", sha="develop_initial_tip"),
        )
    )


def _make_fake_with_pr(*, number: int = 42, head_sha: str = "head_sha_aaa") -> FakeGitHub:
    """Build a FakeGitHub instance seeded with one PR."""
    state = FakeRepoState()
    state.prs[number] = FakePR(
        number=number,
        head_sha=head_sha,
        labels={"mq:queued", "mq:miopen-provider"},
    )
    return FakeGitHub(state)


def _patch_pulls_get_to_return_branch(
    fake: FakeGitHub, *, head_ref: str = "feature-branch"
) -> None:
    """Monkeypatch the fake's pulls.get to add head.ref (the fake stores only sha)."""
    real_get = fake.rest.pulls.get

    def patched_get(owner: str, repo: str, pull_number: int) -> SimpleNamespace:
        resp = real_get(owner, repo, pull_number)
        # The fake returns SimpleNamespace(parsed_data=SimpleNamespace(head=...))
        resp.parsed_data.head.ref = head_ref
        return resp

    fake.rest.pulls.get = patched_get  # type: ignore[assignment]


def _patch_repos_get_branch(
    fake: FakeGitHub, *, branch_sha: str | None = None
) -> None:
    """Add a get_branch method to the fake's repos namespace.

    Returns ``branch_sha`` if provided, else mirrors the fake's current
    ``develop_tip``. Required by ``_handle_squash`` to record the
    pre-squash develop tip for ``_verify_squash``.
    """

    def get_branch(owner: str, repo: str, branch: str) -> SimpleNamespace:
        sha = branch_sha if branch_sha is not None else fake.state.develop_tip
        return SimpleNamespace(
            parsed_data=SimpleNamespace(commit=SimpleNamespace(sha=sha))
        )

    fake.rest.repos.get_branch = get_branch  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Dispatch-table rows: each Action variant must route to its handler.
# ---------------------------------------------------------------------------


def test_dispatch__activate__calls_handle_activate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Activate(pr) dispatches to _handle_activate."""
    from rocm_mq import executor

    called: dict[str, Any] = {}

    def fake_handle_activate(
        pr: PRState, client: Any, config: Any, owner: str, repo: str
    ) -> ActionOutcome:
        called["pr"] = pr
        return ActionOutcome(action=Activate(pr=pr), success=True, error_message=None)

    monkeypatch.setattr(executor, "_handle_activate", fake_handle_activate)
    pr = _make_pr_state()
    outcome = executor.dispatch(
        Activate(pr=pr),
        client=MagicMock(),
        config=canonical_merge_queue_config(),
        owner="org",
        repo="repo",
    )
    assert outcome.success is True
    assert called["pr"] is pr


def test_dispatch__squash__calls_handle_squash(monkeypatch: pytest.MonkeyPatch) -> None:
    """Squash(pr) dispatches to _handle_squash."""
    from rocm_mq import executor

    called: dict[str, Any] = {}

    def fake_handle_squash(
        pr: PRState, client: Any, config: Any, owner: str, repo: str
    ) -> ActionOutcome:
        called["pr"] = pr
        return ActionOutcome(action=Squash(pr=pr), success=True, error_message=None)

    monkeypatch.setattr(executor, "_handle_squash", fake_handle_squash)
    pr = _make_pr_state()
    outcome = executor.dispatch(
        Squash(pr=pr),
        client=MagicMock(),
        config=canonical_merge_queue_config(),
        owner="org",
        repo="repo",
    )
    assert outcome.success is True
    assert called["pr"] is pr


def test_dispatch__eject__calls_handle_eject(monkeypatch: pytest.MonkeyPatch) -> None:
    """Eject(pr, reason) dispatches to _handle_eject."""
    from rocm_mq import executor

    called: dict[str, Any] = {}

    def fake_handle_eject(
        pr: PRState,
        reason: str,
        client: Any,
        config: Any,
        owner: str,
        repo: str,
    ) -> ActionOutcome:
        called["pr"] = pr
        called["reason"] = reason
        return ActionOutcome(
            action=Eject(pr=pr, reason=reason), success=True, error_message=None
        )

    monkeypatch.setattr(executor, "_handle_eject", fake_handle_eject)
    pr = _make_pr_state()
    outcome = executor.dispatch(
        Eject(pr=pr, reason="some reason"),
        client=MagicMock(),
        config=canonical_merge_queue_config(),
        owner="org",
        repo="repo",
    )
    assert outcome.success is True
    assert called["reason"] == "some reason"


def test_dispatch__update_comment__calls_handle_update_comment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UpdateComment(pr, body) dispatches to _handle_update_comment."""
    from rocm_mq import executor

    called: dict[str, Any] = {}

    def fake_handle_update(
        pr: PRState,
        body: str,
        client: Any,
        config: Any,
        owner: str,
        repo: str,
    ) -> ActionOutcome:
        called["body"] = body
        return ActionOutcome(
            action=UpdateComment(pr=pr, new_body=body),
            success=True,
            error_message=None,
        )

    monkeypatch.setattr(executor, "_handle_update_comment", fake_handle_update)
    pr = _make_pr_state()
    outcome = executor.dispatch(
        UpdateComment(pr=pr, new_body="hello"),
        client=MagicMock(),
        config=canonical_merge_queue_config(),
        owner="org",
        repo="repo",
    )
    assert outcome.success is True
    assert called["body"] == "hello"


def test_dispatch__defer__returns_success_without_api_call() -> None:
    """Defer(pr, reason) is a no-op — returns success without touching the client."""
    from rocm_mq import executor

    client = MagicMock()
    pr = _make_pr_state()
    defer = Defer(pr=pr, reason="snapshot stale")
    outcome = executor.dispatch(
        defer,
        client=client,
        config=canonical_merge_queue_config(),
        owner="org",
        repo="repo",
    )
    assert outcome.success is True
    assert outcome.error_message is None
    # No API call of any kind was made — client.rest must not have been touched.
    assert not client.rest.method_calls
    # Same for PartialPRState (Q4 resolution): Defer can carry either shape.
    partial = PartialPRState(number=99, head_sha="z", labels=frozenset())
    defer_partial = Defer(pr=partial, reason="derive failed")
    outcome2 = executor.dispatch(
        defer_partial,
        client=client,
        config=canonical_merge_queue_config(),
        owner="org",
        repo="repo",
    )
    assert outcome2.success is True


# ---------------------------------------------------------------------------
# Activation state machine (RFC §4.9)
# ---------------------------------------------------------------------------


def test_activate__repos_merge_201__stamps_status_and_flips_labels() -> None:
    """201 (new merge commit) → stamp status on the new merge SHA + flip labels."""
    from rocm_mq import executor

    fake = _make_fake_with_pr(number=42, head_sha="head_sha_aaa")
    _patch_pulls_get_to_return_branch(fake, head_ref="feature-branch")
    # develop_tip != PR head_sha → merge returns 201 with a new SHA;
    # the fake mutates develop_tip but the PR's head_sha stays the same.
    config = canonical_merge_queue_config()
    pr = _make_pr_state(number=42, head_sha="head_sha_aaa")

    outcome = executor.dispatch(
        Activate(pr=pr),
        client=fake,
        config=config,
        owner="org",
        repo="repo",
    )

    assert outcome.success is True
    # Label flip — mq:active added, mq:queued removed.
    assert "mq:active" in fake.state.prs[42].labels
    assert "mq:queued" not in fake.state.prs[42].labels
    # Status stamped with state=success on the merged SHA.
    # The fake sets develop_tip to "merge_<branch>_<head>" on 201; the stamp goes
    # on that merged SHA.
    statuses = [s for (sha, ctx), s in fake.state.status_store.items()
                if ctx == config.activation_status_context]
    assert len(statuses) == 1, statuses
    assert statuses[0]["state"] == "success"


def test_activate__repos_merge_204__uses_current_head_sha() -> None:
    """204 (already up-to-date) → stamp uses the PR's current head SHA."""
    from rocm_mq import executor

    fake = _make_fake_with_pr(number=42, head_sha="head_sha_aaa")
    # Force the fake's merge to return 204 by aligning develop_tip with the PR head.
    fake.state.develop_tip = "head_sha_aaa"
    _patch_pulls_get_to_return_branch(fake, head_ref="feature-branch")
    config = canonical_merge_queue_config()
    pr = _make_pr_state(number=42, head_sha="head_sha_aaa")

    outcome = executor.dispatch(
        Activate(pr=pr),
        client=fake,
        config=config,
        owner="org",
        repo="repo",
    )

    assert outcome.success is True
    # Status stamped on the current head SHA (since merge was a no-op).
    assert ("head_sha_aaa", config.activation_status_context) in fake.state.status_store


def test_activate__repos_merge_409__returns_failure_outcome() -> None:
    """409 conflict on develop→PR merge → ActionOutcome(success=False)."""
    from rocm_mq import executor

    fake = _make_fake_with_pr(number=42, head_sha="head_sha_aaa")
    _patch_pulls_get_to_return_branch(fake, head_ref="feature-branch")

    def conflict_merge(*args: Any, **kwargs: Any) -> SimpleNamespace:
        raise _make_request_failed(409)

    fake.rest.repos.merge = conflict_merge  # type: ignore[assignment]
    config = canonical_merge_queue_config()
    pr = _make_pr_state(number=42, head_sha="head_sha_aaa")

    outcome = executor.dispatch(
        Activate(pr=pr),
        client=fake,
        config=config,
        owner="org",
        repo="repo",
    )

    assert outcome.success is False
    assert outcome.error_message is not None
    assert "conflict" in outcome.error_message.lower()
    # No status stamped, no labels flipped.
    assert not fake.state.status_store
    assert "mq:active" not in fake.state.prs[42].labels


def test_activate__pre_stamp_race__author_pushed_between_merge_and_stamp() -> None:
    """201 merge succeeds, then PR head advances → abort stamp (no stale SHA)."""
    from rocm_mq import executor

    fake = _make_fake_with_pr(number=42, head_sha="head_sha_aaa")
    config = canonical_merge_queue_config()

    # Stub pulls.get to return a head SHA DIFFERENT from the merge SHA returned
    # by repos.merge.  Real merge will produce sha="merge_feature-branch_develop_initial_tip"
    # but we want pulls.get's post-merge re-read to show the author has pushed.
    def racing_pulls_get(owner: str, repo: str, pull_number: int) -> SimpleNamespace:
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                head=SimpleNamespace(sha="race_new_sha", ref="feature-branch"),
                base=SimpleNamespace(ref="develop", sha="develop_initial_tip"),
            )
        )

    fake.rest.pulls.get = racing_pulls_get  # type: ignore[assignment]
    pr = _make_pr_state(number=42, head_sha="head_sha_aaa")

    outcome = executor.dispatch(
        Activate(pr=pr),
        client=fake,
        config=config,
        owner="org",
        repo="repo",
    )

    assert outcome.success is False
    assert outcome.error_message is not None
    assert "race" in outcome.error_message.lower() or "advanced" in outcome.error_message.lower()
    # Crucially: NO status stamped (we never call create_commit_status with a
    # stale SHA).
    assert not fake.state.status_store


def test_activate__second_call__noop_on_already_active_pr() -> None:
    """Second activate on an already-active PR completes without error (UK-3)."""
    from rocm_mq import executor

    fake = _make_fake_with_pr(number=42, head_sha="head_sha_aaa")
    _patch_pulls_get_to_return_branch(fake, head_ref="feature-branch")
    config = canonical_merge_queue_config()
    pr = _make_pr_state(number=42, head_sha="head_sha_aaa")

    # First activate.
    out1 = executor.dispatch(
        Activate(pr=pr), client=fake, config=config, owner="org", repo="repo"
    )
    assert out1.success is True

    # Second activate — the fake's develop now contains the PR (develop_tip
    # was updated); fake's merge() returns 204; label add is idempotent;
    # status create overwrites.
    # Re-align develop_tip so the second merge call returns 204.
    fake.state.develop_tip = "head_sha_aaa"
    out2 = executor.dispatch(
        Activate(pr=pr), client=fake, config=config, owner="org", repo="repo"
    )
    assert out2.success is True
    # Still exactly the active label set (no duplicates).
    assert fake.state.prs[42].labels == {"mq:active", "mq:miopen-provider"}


# ---------------------------------------------------------------------------
# Per-handler idempotency (UK-3 second-call scenarios)
# ---------------------------------------------------------------------------


def test_idempotency__remove_label_404__treated_as_noop() -> None:
    """_safe_remove_label catches 404 (label absent) and returns success."""
    from rocm_mq import executor

    fake = _make_fake_with_pr(number=42, head_sha="head_sha_aaa")
    config = canonical_merge_queue_config()
    # Make sure mq:queued is NOT on the PR — removal will 404.
    fake.state.prs[42].labels.discard("mq:queued")

    # Eject handler removes all mq:* labels via _safe_remove_label; since
    # mq:queued is absent we should NOT crash.
    pr = _make_pr_state(
        number=42,
        head_sha="head_sha_aaa",
        labels=frozenset({"mq:active", "mq:miopen-provider"}),
    )
    outcome = executor.dispatch(
        Eject(pr=pr, reason="testing 404 swallow"),
        client=fake,
        config=config,
        owner="org",
        repo="repo",
    )

    assert outcome.success is True


def test_idempotency__squash_already_merged__405_treated_as_noop() -> None:
    """pulls.merge raising 405 (already merged) → ActionOutcome(success=True)."""
    from rocm_mq import executor

    fake = _make_fake_with_pr(number=42, head_sha="head_sha_aaa")
    fake.state.prs[42].merged = True  # second call → 405
    _patch_repos_get_branch(fake)
    config = canonical_merge_queue_config()
    pr = _make_pr_state(number=42, head_sha="head_sha_aaa")

    outcome = executor.dispatch(
        Squash(pr=pr), client=fake, config=config, owner="org", repo="repo"
    )

    assert outcome.success is True
    # error_message documents the no-op.
    assert outcome.error_message is not None
    assert "merged" in outcome.error_message.lower()


def test_idempotency__status_overwrite__no_error_on_second_post() -> None:
    """create_commit_status called twice on same (SHA, context) — second succeeds."""
    from rocm_mq import executor

    fake = _make_fake_with_pr(number=42, head_sha="head_sha_aaa")
    _patch_pulls_get_to_return_branch(fake, head_ref="feature-branch")
    config = canonical_merge_queue_config()
    pr = _make_pr_state(number=42, head_sha="head_sha_aaa")

    # First and second activate; both must succeed and the status_store still
    # carries exactly one entry per (sha, context) — overwrite semantics.
    out1 = executor.dispatch(
        Activate(pr=pr), client=fake, config=config, owner="org", repo="repo"
    )
    fake.state.develop_tip = "head_sha_aaa"  # second merge → 204
    out2 = executor.dispatch(
        Activate(pr=pr), client=fake, config=config, owner="org", repo="repo"
    )

    assert out1.success is True
    assert out2.success is True
    # Status keys are (sha, context); overwrite means each key is unique.
    keys = list(fake.state.status_store.keys())
    assert len(keys) == len(set(keys))


def test_idempotency__add_labels__already_present__no_duplicate() -> None:
    """add_labels on already-present label is a no-op — set semantics in fake."""
    from rocm_mq import executor

    fake = _make_fake_with_pr(number=42, head_sha="head_sha_aaa")
    # Pre-seed mq:active so the activate handler's add_labels is a re-add.
    fake.state.prs[42].labels.add("mq:active")
    _patch_pulls_get_to_return_branch(fake, head_ref="feature-branch")
    config = canonical_merge_queue_config()
    pr = _make_pr_state(number=42, head_sha="head_sha_aaa")

    outcome = executor.dispatch(
        Activate(pr=pr), client=fake, config=config, owner="org", repo="repo"
    )
    assert outcome.success is True
    # mq:active still present, exactly once (set membership).
    assert "mq:active" in fake.state.prs[42].labels


def test_idempotency__repos_merge_204__treated_as_success() -> None:
    """repos.merge returning 204 → handler treats as 'already up-to-date'."""
    from rocm_mq import executor

    fake = _make_fake_with_pr(number=42, head_sha="head_sha_aaa")
    fake.state.develop_tip = "head_sha_aaa"  # forces 204
    _patch_pulls_get_to_return_branch(fake, head_ref="feature-branch")
    config = canonical_merge_queue_config()
    pr = _make_pr_state(number=42, head_sha="head_sha_aaa")

    outcome = executor.dispatch(
        Activate(pr=pr), client=fake, config=config, owner="org", repo="repo"
    )
    assert outcome.success is True
    # The stamp uses the PR's current head SHA.
    assert ("head_sha_aaa", config.activation_status_context) in fake.state.status_store


# ---------------------------------------------------------------------------
# Post-squash verification — IO-05 / Pitfall 8
# ---------------------------------------------------------------------------


def test_verify_squash__parent_sha_matches__returns_ok() -> None:
    """_verify_squash returns without raising when parents[0].sha matches expected."""
    from rocm_mq import executor

    fake = _make_fake_with_pr(number=42, head_sha="head_sha_aaa")
    # Seed a commit whose parents[0].sha matches our recorded develop tip.
    fake.state.commits["squash_sha_123"] = {
        "parents": ["develop_tip_abc"],
        "message": "Squash #42",
    }
    pr = _make_pr_state(number=42, head_sha="head_sha_aaa")

    # Should NOT raise.
    executor._verify_squash(
        client=fake,
        owner="org",
        repo="repo",
        pr=pr,
        pre_squash_develop_sha="develop_tip_abc",
        squash_sha="squash_sha_123",
    )


def test_verify_squash__parent_sha_mismatch__raises_corrupt_squash_error() -> None:
    """_verify_squash raises CorruptSquashError when parents[0].sha != expected."""
    from rocm_mq import executor

    fake = _make_fake_with_pr(number=42, head_sha="head_sha_aaa")
    fake.state.commits["squash_sha_456"] = {
        "parents": ["UNEXPECTED_PARENT_SHA"],
        "message": "Squash #42",
    }
    pr = _make_pr_state(number=42, head_sha="head_sha_aaa")

    with pytest.raises(CorruptSquashError) as exc_info:
        executor._verify_squash(
            client=fake,
            owner="org",
            repo="repo",
            pr=pr,
            pre_squash_develop_sha="develop_tip_abc",
            squash_sha="squash_sha_456",
        )
    msg = str(exc_info.value)
    assert "42" in msg  # PR number visible in message
    assert "develop_tip_abc" in msg or "UNEXPECTED_PARENT_SHA" in msg


def test_verify_squash__get_commit_404_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Replication lag: first get_commit raises 404, second succeeds."""
    from rocm_mq import executor

    monkeypatch.setattr("rocm_mq.executor.time.sleep", lambda _: None)

    # Build a fake whose get_commit raises 404 once, then returns the commit.
    fake = _make_fake_with_pr(number=42, head_sha="head_sha_aaa")
    fake.state.commits["squash_sha_789"] = {
        "parents": ["develop_tip_xyz"],
        "message": "Squash #42",
    }
    attempts = {"n": 0}
    real_get_commit = fake.rest.repos.get_commit

    def flaky_get_commit(owner: str, repo: str, ref: str) -> SimpleNamespace:
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise _make_request_failed(404)
        return real_get_commit(owner, repo, ref)

    fake.rest.repos.get_commit = flaky_get_commit  # type: ignore[assignment]
    pr = _make_pr_state(number=42, head_sha="head_sha_aaa")

    # Should succeed on second attempt.
    executor._verify_squash(
        client=fake,
        owner="org",
        repo="repo",
        pr=pr,
        pre_squash_develop_sha="develop_tip_xyz",
        squash_sha="squash_sha_789",
    )
    assert attempts["n"] == 2


def test_verify_squash__get_commit_404_three_times__raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three retries return 404 → propagate RequestFailed."""
    from rocm_mq import executor

    monkeypatch.setattr("rocm_mq.executor.time.sleep", lambda _: None)

    fake = _make_fake_with_pr(number=42, head_sha="head_sha_aaa")
    attempts = {"n": 0}

    def always_404(owner: str, repo: str, ref: str) -> SimpleNamespace:
        attempts["n"] += 1
        raise _make_request_failed(404)

    fake.rest.repos.get_commit = always_404  # type: ignore[assignment]
    pr = _make_pr_state(number=42, head_sha="head_sha_aaa")

    with pytest.raises(RequestFailed):
        executor._verify_squash(
            client=fake,
            owner="org",
            repo="repo",
            pr=pr,
            pre_squash_develop_sha="develop_tip_xyz",
            squash_sha="squash_sha_missing",
        )
    assert attempts["n"] == 3


def test_handle_squash__verify_failure__returns_failure_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """dispatch(Squash) returns success=False when _verify_squash raises."""
    from rocm_mq import executor

    monkeypatch.setattr("rocm_mq.executor.time.sleep", lambda _: None)

    fake = _make_fake_with_pr(number=42, head_sha="head_sha_aaa")
    _patch_repos_get_branch(fake)  # captures pre-squash develop tip
    config = canonical_merge_queue_config()
    pr = _make_pr_state(number=42, head_sha="head_sha_aaa")

    # Sabotage post-squash get_commit so parents[0] is wrong (silent corruption).
    def corrupted_get_commit(owner: str, repo: str, ref: str) -> SimpleNamespace:
        return SimpleNamespace(
            parsed_data=SimpleNamespace(
                sha=ref,
                parents=[SimpleNamespace(sha="WRONG_PARENT_SHA")],
            )
        )

    fake.rest.repos.get_commit = corrupted_get_commit  # type: ignore[assignment]

    outcome = executor.dispatch(
        Squash(pr=pr), client=fake, config=config, owner="org", repo="repo"
    )

    assert outcome.success is False
    assert outcome.error_message is not None
    # Pitfall 8 — error message preserves the corrupt-squash context.
    assert "parent" in outcome.error_message.lower() or "squash" in outcome.error_message.lower()


# ---------------------------------------------------------------------------
# _find_status_comment_id — lazy marker discovery
# ---------------------------------------------------------------------------


def test_find_status_comment_id__comment_with_marker__returns_id() -> None:
    """A comment containing the <!-- rocm-mq-status --> marker is returned by id."""
    from rocm_mq import executor

    client = MagicMock()
    client.rest.issues.list_comments.return_value = SimpleNamespace(
        parsed_data=[
            SimpleNamespace(id=1001, body="Random review comment"),
            SimpleNamespace(id=1002, body="Build report\n<!-- rocm-mq-status -->\n..."),
            SimpleNamespace(id=1003, body="Another comment"),
        ]
    )
    found = executor._find_status_comment_id(client, "org", "repo", 42)
    assert found == 1002


def test_find_status_comment_id__no_marker__returns_none() -> None:
    """No comment with the marker → returns None."""
    from rocm_mq import executor

    client = MagicMock()
    client.rest.issues.list_comments.return_value = SimpleNamespace(
        parsed_data=[
            SimpleNamespace(id=1001, body="Random review comment"),
            SimpleNamespace(id=1002, body="No marker here"),
        ]
    )
    found = executor._find_status_comment_id(client, "org", "repo", 42)
    assert found is None


# ---------------------------------------------------------------------------
# UpdateComment end-to-end (create vs update path)
# ---------------------------------------------------------------------------


def test_update_comment__no_existing_comment__creates_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If no status comment exists, _handle_update_comment calls create_comment."""
    from rocm_mq import executor

    monkeypatch.setattr(
        executor, "_find_status_comment_id", lambda *a, **kw: None
    )
    client = MagicMock()
    client.rest.issues.create_comment.return_value = SimpleNamespace(
        parsed_data=SimpleNamespace(id=2001, body="x")
    )
    pr = _make_pr_state()
    outcome = executor.dispatch(
        UpdateComment(pr=pr, new_body="hello"),
        client=client,
        config=canonical_merge_queue_config(),
        owner="org",
        repo="repo",
    )
    assert outcome.success is True
    client.rest.issues.create_comment.assert_called_once()


def test_update_comment__existing_comment__updates_in_place(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a status comment exists, _handle_update_comment calls update_comment."""
    from rocm_mq import executor

    monkeypatch.setattr(
        executor, "_find_status_comment_id", lambda *a, **kw: 9999
    )
    client = MagicMock()
    client.rest.issues.update_comment.return_value = SimpleNamespace(
        parsed_data=SimpleNamespace(id=9999, body="updated")
    )
    pr = _make_pr_state()
    outcome = executor.dispatch(
        UpdateComment(pr=pr, new_body="updated"),
        client=client,
        config=canonical_merge_queue_config(),
        owner="org",
        repo="repo",
    )
    assert outcome.success is True
    client.rest.issues.update_comment.assert_called_once()
    client.rest.issues.create_comment.assert_not_called()


# ---------------------------------------------------------------------------
# Eject handler — status overwrite + label cleanup
# ---------------------------------------------------------------------------


def test_eject__overwrites_status_to_failure_and_cleans_mq_labels() -> None:
    """Eject stamps merge-queue/active=failure and removes all mq:* labels."""
    from rocm_mq import executor

    fake = _make_fake_with_pr(number=42, head_sha="head_sha_aaa")
    config = canonical_merge_queue_config()
    # PR carries multiple mq:* labels and one non-mq label.
    fake.state.prs[42].labels = {"mq:queued", "mq:active", "mq:miopen-provider", "needs-review"}
    pr = _make_pr_state(
        number=42,
        head_sha="head_sha_aaa",
        labels=frozenset(fake.state.prs[42].labels),
    )

    outcome = executor.dispatch(
        Eject(pr=pr, reason="CI failed"),
        client=fake,
        config=config,
        owner="org",
        repo="repo",
    )

    assert outcome.success is True
    # Status: failure on the PR's head SHA, with the activation context.
    entry = fake.state.status_store[("head_sha_aaa", config.activation_status_context)]
    assert entry["state"] == "failure"
    # All mq:* labels removed; non-mq labels preserved.
    remaining = fake.state.prs[42].labels
    assert "needs-review" in remaining
    assert not any(label.startswith("mq:") for label in remaining)
