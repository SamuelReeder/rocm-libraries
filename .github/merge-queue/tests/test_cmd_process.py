"""
tests/test_cmd_process.py — End-to-end tests for the rocm_mq process-cycle CLI.

Covers IO-07 (cmd_process orchestrates build_snapshot → derive_snapshot →
decide_cycle → dispatch end-to-end against ``FakeGitHub``) and DOG-01 ("no real
GitHub API call" property — the full read→decide→execute chain runs without
touching the network).

Test strategy:
- Drive ``cmd_process.process_cycle`` directly (not via subprocess) for speed.
- Use ``FakeGitHub`` (tests.gh_fake) as the client; seed ``FakeRepoState``
  per scenario; assert mutations land on the fake.
- DOG-01 assertion: monkeypatch ``httpx.Client.send`` to fail loudly if any
  real HTTP request is attempted in --fake mode.
- ``main()`` tests use ``monkeypatch.setattr(sys, "argv", ...)`` and assert
  exit behaviour.

Scenario builder helpers below construct the minimal ``FakeRepoState`` needed
to exercise each path through the cycle without depending on the full set of
production data shapes.
"""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

from rocm_mq.gh import CorruptSquashError
from tests.conftest import canonical_merge_queue_config, utc
from tests.gh_fake import FakeGitHub, FakePR, FakeRepoState

# ---------------------------------------------------------------------------
# Scenario builders
# ---------------------------------------------------------------------------


def _seed_activate_scenario() -> FakeGitHub:
    """Build a FakeGitHub with one PR ready to be ACTIVATED.

    The PR carries ``mq:queued`` + ``mq:miopen-provider`` labels so the search
    discovers it; the App-applied ``mq:queued`` labelling event is pre-seeded
    in ``state.label_log`` so ``derive_pr`` treats the PR as Case 1 (normal,
    not deferred) and ``decide_cycle`` emits ``Activate(pr)``.

    The fake's ``_patch_pulls_get_to_return_branch`` /
    ``_patch_repos_merge_to_advance_pr_head`` shims (see test_executor.py)
    are inlined here so ``_handle_activate`` can complete its state machine.
    """
    state = FakeRepoState(develop_tip="develop_initial_tip")
    state.prs[42] = FakePR(
        number=42,
        head_sha="head_sha_42",
        labels={"mq:queued", "mq:miopen-provider"},
    )
    # Pre-seed the App-applied mq:queued label event so derive_pr's Case 1
    # path is taken (otherwise Case 2 emits a Defer and no Activate fires).
    state.label_log.append(("42", "labeled", "mq:queued"))
    fake = FakeGitHub(state)
    _patch_pulls_get_for_branch_ref(fake, head_ref="feature-branch")
    _patch_repos_merge_to_advance_pr_head(fake, 42)
    return fake


def _patch_pulls_get_for_branch_ref(
    fake: FakeGitHub, *, head_ref: str = "feature-branch"
) -> None:
    """Add ``head.ref`` to the fake's pulls.get response (executor reads it)."""
    real_get = fake.rest.pulls.get

    def patched_get(owner: str, repo: str, pull_number: int) -> Any:
        resp = real_get(owner, repo, pull_number)
        resp.parsed_data.head.ref = head_ref
        return resp

    fake.rest.pulls.get = patched_get  # type: ignore[assignment]


def _patch_repos_merge_to_advance_pr_head(fake: FakeGitHub, pr_number: int) -> None:
    """Make repos.merge 201 also advance the PR head_sha (real-GitHub behaviour)."""
    real_merge = fake.rest.repos.merge

    def patched_merge(owner: str, repo: str, **kwargs: Any) -> Any:
        resp = real_merge(owner, repo, **kwargs)
        if resp.status_code == 201 and resp.parsed_data is not None:
            fake.state.prs[pr_number].head_sha = str(resp.parsed_data.sha)
        return resp

    fake.rest.repos.merge = patched_merge  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 1. End-to-end activate scenario — labels flip, status posted
# ---------------------------------------------------------------------------


def test_process_cycle__fake__activate_scenario() -> None:
    """A PR with mq:queued + mq:<queue> labels gets activated end-to-end.

    After process_cycle, the fake's status_store contains the activation
    status on the PR's new head SHA and the PR labels include mq:active
    (mq:queued removed).
    """
    from rocm_mq import cmd_process

    fake = _seed_activate_scenario()
    config = canonical_merge_queue_config()
    outcomes = cmd_process.process_cycle(
        client=fake,
        config=config,
        owner="SamuelReeder",
        repo="rocm-libraries",
        dry_run=False,
        now=utc(2026, 5, 18, 10, 0),
    )

    # At least one Activate outcome.
    assert any(o.success for o in outcomes), (
        f"Expected at least one successful action, got: {outcomes}"
    )

    # Activation status posted on the new head SHA (the merge sha after 201).
    activation_ctx = config.activation_status_context
    new_head = fake.state.prs[42].head_sha
    assert (new_head, activation_ctx) in fake.state.status_store, (
        f"Expected activation status at ({new_head!r}, {activation_ctx!r}); "
        f"status_store keys: {list(fake.state.status_store)}"
    )

    # Labels flipped.
    pr_labels = fake.state.prs[42].labels
    assert "mq:active" in pr_labels, f"Expected mq:active in {pr_labels}"
    assert "mq:queued" not in pr_labels, f"Expected mq:queued removed, got {pr_labels}"


# ---------------------------------------------------------------------------
# 2. DOG-01: no real HTTP requests in --fake mode
# ---------------------------------------------------------------------------


def test_process_cycle__fake__no_real_api_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """The full --fake cycle MUST NOT make any real HTTP request.

    Monkeypatches httpx.Client.send to raise; if process_cycle attempts a real
    HTTP call (e.g., because someone wired a real GitHubClient through the
    --fake path), this test detonates loudly.
    """
    import httpx

    from rocm_mq import cmd_process

    def boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "process_cycle(--fake) attempted a real HTTP request — "
            "DOG-01 violation"
        )

    monkeypatch.setattr(httpx.Client, "send", boom)
    # Also guard the async client for defence in depth.
    monkeypatch.setattr(httpx.AsyncClient, "send", boom)

    fake = _seed_activate_scenario()
    cmd_process.process_cycle(
        client=fake,
        config=canonical_merge_queue_config(),
        owner="SamuelReeder",
        repo="rocm-libraries",
        dry_run=False,
        now=utc(2026, 5, 18, 10, 0),
    )


# ---------------------------------------------------------------------------
# 3. --dry-run skips dispatch and emits actions to stdout
# ---------------------------------------------------------------------------


def test_process_cycle__fake__dry_run__no_mutations(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With dry_run=True, no GitHub mutations happen and actions print to stdout."""
    from rocm_mq import cmd_process

    fake = _seed_activate_scenario()
    config = canonical_merge_queue_config()
    outcomes = cmd_process.process_cycle(
        client=fake,
        config=config,
        owner="SamuelReeder",
        repo="rocm-libraries",
        dry_run=True,
        now=utc(2026, 5, 18, 10, 0),
    )

    # No outcomes (dispatch never called).
    assert outcomes == (), f"dry_run should return empty outcomes, got: {outcomes}"

    # No status posted.
    assert fake.state.status_store == {}, (
        f"dry_run must not mutate status_store, got: {fake.state.status_store}"
    )

    # PR labels untouched.
    assert fake.state.prs[42].labels == {"mq:queued", "mq:miopen-provider"}, (
        f"dry_run must not mutate labels, got: {fake.state.prs[42].labels}"
    )

    # stdout contains an action name (the would-be action).
    captured = capsys.readouterr()
    assert "Activate" in captured.out, (
        f"dry_run should print Activate to stdout, got: {captured.out!r}"
    )


# ---------------------------------------------------------------------------
# 4. Empty queue — no actions
# ---------------------------------------------------------------------------


def test_process_cycle__fake__empty_queue__no_actions() -> None:
    """An empty FakeRepoState yields zero actions and zero outcomes."""
    from rocm_mq import cmd_process

    fake = FakeGitHub(FakeRepoState())
    outcomes = cmd_process.process_cycle(
        client=fake,
        config=canonical_merge_queue_config(),
        owner="SamuelReeder",
        repo="rocm-libraries",
        dry_run=False,
        now=utc(2026, 5, 18, 10, 0),
    )
    assert outcomes == (), f"Expected empty outcomes for empty queue, got: {outcomes}"


# ---------------------------------------------------------------------------
# 5. CorruptSquashError surfaces from _handle_squash
# ---------------------------------------------------------------------------


def test_process_cycle__fake__corrupt_squash__returns_failure_outcome(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A CorruptSquashError from _handle_squash propagates as success=False.

    _handle_squash catches CorruptSquashError and converts to an
    ActionOutcome(success=False, error_message=str(exc)). main() must detect
    the failed Squash outcome and exit non-zero.
    """
    from rocm_mq import cmd_process, executor

    # Force the squash path: monkeypatch decide_cycle to emit a Squash for our
    # seeded PR (the activation-scenario seeded PR is not yet active, so the
    # natural decide_cycle output is Activate, not Squash).
    from rocm_mq.state import (
        ActionOutcome,
        PRState,
        Squash,
    )

    pr = PRState(
        number=42,
        head_sha="head_sha_42",
        labels=frozenset({"mq:active", "mq:miopen-provider"}),
        queues=frozenset({"miopen-provider"}),
        enqueued_at=utc(2026, 5, 18, 9, 0),
        is_validly_active=True,
        required_check_results=(),
    )

    def fake_decide_cycle(snapshot: Any, config: Any, now: Any) -> list[Any]:
        return [Squash(pr=pr)]

    def fake_derive_snapshot(raw: Any, config: Any, now: Any) -> Any:
        from rocm_mq.state import Snapshot

        return (Snapshot(prs=()), ())

    monkeypatch.setattr(cmd_process, "decide_cycle", fake_decide_cycle)
    monkeypatch.setattr(cmd_process, "derive_snapshot", fake_derive_snapshot)

    # Patch _handle_squash to return a CorruptSquashError-bearing outcome.
    def fake_handle_squash(*args: Any, **kwargs: Any) -> ActionOutcome:
        return ActionOutcome(
            action=Squash(pr=pr),
            success=False,
            error_message=str(
                CorruptSquashError(
                    "PR #42 squash 'squash_42': expected parent 'a', got 'b'"
                )
            ),
        )

    monkeypatch.setattr(executor, "_handle_squash", fake_handle_squash)

    fake = FakeGitHub(FakeRepoState())
    outcomes = cmd_process.process_cycle(
        client=fake,
        config=canonical_merge_queue_config(),
        owner="SamuelReeder",
        repo="rocm-libraries",
        dry_run=False,
        now=utc(2026, 5, 18, 10, 0),
    )
    assert len(outcomes) == 1
    assert outcomes[0].success is False
    assert "expected parent" in (outcomes[0].error_message or "")


# ---------------------------------------------------------------------------
# 6. main() --fake flag wiring
# ---------------------------------------------------------------------------


def test_main__fake_flag__calls_process_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main() with --fake constructs a FakeGitHub and calls process_cycle."""
    from rocm_mq import cmd_process

    called: dict[str, Any] = {}

    def fake_process_cycle(**kwargs: Any) -> tuple[Any, ...]:
        called["kwargs"] = kwargs
        return ()

    monkeypatch.setattr(cmd_process, "process_cycle", fake_process_cycle)
    monkeypatch.setattr(
        sys,
        "argv",
        ["rocm-mq", "process-cycle", "--fake", "--repo", "SamuelReeder/rocm-libraries"],
    )
    rc = cmd_process.main()
    assert rc == 0
    assert called["kwargs"]["owner"] == "SamuelReeder"
    assert called["kwargs"]["repo"] == "rocm-libraries"
    assert called["kwargs"]["dry_run"] is False
    # Client should be a FakeGitHub (not a real GitHubClient).
    assert isinstance(called["kwargs"]["client"], FakeGitHub)


# ---------------------------------------------------------------------------
# 7. main() without --fake and without GITHUB_TOKEN — non-zero exit
# ---------------------------------------------------------------------------


def test_main__missing_token_without_fake__exits_nonzero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without --fake and without GITHUB_TOKEN, main() must exit non-zero."""
    from rocm_mq import cmd_process

    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.setattr(
        sys,
        "argv",
        ["rocm-mq", "process-cycle", "--repo", "SamuelReeder/rocm-libraries"],
    )
    rc = cmd_process.main()
    assert rc != 0, f"Expected non-zero exit without GITHUB_TOKEN, got {rc}"


# ---------------------------------------------------------------------------
# 8. $GITHUB_STEP_SUMMARY written on success
# ---------------------------------------------------------------------------


def test_github_step_summary__written_on_success(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
) -> None:
    """When $GITHUB_STEP_SUMMARY is set, process_cycle appends the rendered summary."""
    from rocm_mq import cmd_process

    summary_file = tmp_path / "step_summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary_file))

    fake = _seed_activate_scenario()
    cmd_process.process_cycle(
        client=fake,
        config=canonical_merge_queue_config(),
        owner="SamuelReeder",
        repo="rocm-libraries",
        dry_run=False,
        now=utc(2026, 5, 18, 10, 0),
    )

    assert summary_file.exists(), "$GITHUB_STEP_SUMMARY path was not written"
    content = summary_file.read_text()
    assert "Queue depth" in content, (
        f"Step summary missing 'Queue depth' header, got: {content!r}"
    )
    assert "Cycle duration" in content, (
        f"Step summary missing 'Cycle duration' header, got: {content!r}"
    )


# ---------------------------------------------------------------------------
# 9. Bonus: --dry-run + --fake together still skips dispatch
# ---------------------------------------------------------------------------


def test_main__dry_run_fake__no_state_mutation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """main() with --dry-run --fake completes 0 and leaves state untouched."""
    from rocm_mq import cmd_process

    fake = _seed_activate_scenario()

    # Capture the fake the CLI constructs; replace _build_fake_client so we
    # can inspect it after main() returns.
    monkeypatch.setattr(cmd_process, "_build_fake_client", lambda: fake)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "rocm-mq",
            "process-cycle",
            "--fake",
            "--dry-run",
            "--repo",
            "SamuelReeder/rocm-libraries",
        ],
    )
    rc = cmd_process.main()
    assert rc == 0
    # No mutations.
    assert fake.state.status_store == {}
    assert fake.state.prs[42].labels == {"mq:queued", "mq:miopen-provider"}


# ---------------------------------------------------------------------------
# 10. argparse: --help exits zero (smoke test on parser construction)
# ---------------------------------------------------------------------------


def test_main__help__exits_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    """argparse --help triggers SystemExit(0); main() surfaces that cleanly."""
    from rocm_mq import cmd_process

    monkeypatch.setattr(sys, "argv", ["rocm-mq", "process-cycle", "--help"])
    with pytest.raises(SystemExit) as exc_info:
        cmd_process.main()
    assert exc_info.value.code == 0


# ---------------------------------------------------------------------------
# 11. process_cycle uses the provided ``now`` (no datetime.now() inside cycle)
# ---------------------------------------------------------------------------


def test_process_cycle__now_threaded_through_to_decide_cycle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ``now`` argument must be passed to derive_snapshot AND decide_cycle.

    Pinning this contract guards the PATTERNS.md "now threading" rule: only
    cmd_process is allowed to call datetime.now(tz=UTC) at cycle scope.
    """
    from rocm_mq import cmd_process

    observed: dict[str, Any] = {}

    def spy_derive(raw: Any, config: Any, now: Any) -> Any:
        observed["derive_now"] = now
        from rocm_mq.state import Snapshot

        return Snapshot(prs=()), ()

    def spy_decide(snapshot: Any, config: Any, now: Any) -> list[Any]:
        observed["decide_now"] = now
        return []

    monkeypatch.setattr(cmd_process, "derive_snapshot", spy_derive)
    monkeypatch.setattr(cmd_process, "decide_cycle", spy_decide)

    fake = FakeGitHub(FakeRepoState())
    fixed_now = utc(2026, 5, 18, 10, 0)
    cmd_process.process_cycle(
        client=fake,
        config=canonical_merge_queue_config(),
        owner="x",
        repo="y",
        dry_run=False,
        now=fixed_now,
    )
    assert observed["derive_now"] == fixed_now
    assert observed["decide_now"] == fixed_now


# ---------------------------------------------------------------------------
# 12. Pre-defers from derive_snapshot surface as Defer actions
# ---------------------------------------------------------------------------


def test_process_cycle__pre_defers_emitted_as_defer_actions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When derive_snapshot returns pre-defers, they must appear in outcomes."""
    from rocm_mq import cmd_process
    from rocm_mq.state import Defer, PartialPRState, Snapshot

    pre_defer = Defer(
        pr=PartialPRState(
            number=99, head_sha="sha99", labels=frozenset({"mq:queued"})
        ),
        reason="mq:queued label present but timeline event not yet visible",
    )

    def fake_derive(raw: Any, config: Any, now: Any) -> Any:
        return Snapshot(prs=()), (pre_defer,)

    def fake_decide(snapshot: Any, config: Any, now: Any) -> list[Any]:
        return []

    monkeypatch.setattr(cmd_process, "derive_snapshot", fake_derive)
    monkeypatch.setattr(cmd_process, "decide_cycle", fake_decide)

    fake = FakeGitHub(FakeRepoState())
    outcomes = cmd_process.process_cycle(
        client=fake,
        config=canonical_merge_queue_config(),
        owner="x",
        repo="y",
        dry_run=False,
        now=utc(2026, 5, 18, 10, 0),
    )
    assert len(outcomes) == 1
    assert outcomes[0].action == pre_defer
    assert outcomes[0].success is True


# ---------------------------------------------------------------------------
# Module-level smoke: cmd_process imports cleanly (RED phase verifies this fails)
# ---------------------------------------------------------------------------


def test_cmd_process_module_importable() -> None:
    """cmd_process module exists and exposes main + process_cycle."""
    from rocm_mq import cmd_process

    assert callable(cmd_process.main)
    assert callable(cmd_process.process_cycle)


# Defensive — MagicMock is imported so static linters don't drop the import.
_ = MagicMock
