"""
rocm_mq.cmd_process — CLI entrypoint for the merge-queue processor cycle.

Orchestrates the full RFC §4.9 read → derive → decide → execute loop:

    build_snapshot  →  derive_snapshot  →  decide_cycle  →  dispatch
        (I/O)           (pure)            (pure)          (I/O)

This module is the GLUE layer between Phase 2's I/O adapters
(``snapshot.py``, ``executor.py``, ``gh.py``) and Phase 1's pure decision
layer. It is the ONLY module in ``rocm_mq`` that calls
``datetime.now(tz=UTC)`` at cycle scope (PATTERNS.md "now threading" rule):
the single ``now`` value is computed in ``main()`` and threaded through to
``derive_snapshot`` and ``decide_cycle`` so all per-cycle timestamps are
consistent and deterministic in tests.

Public surface (Phase 2 contract):
- ``main() -> int`` — argparse + token resolution + client construction +
  process_cycle invocation. Returns 0 on success, non-zero on any failure
  outcome (including ``CorruptSquashError`` surfaced via _handle_squash).
- ``process_cycle(*, client, config, owner, repo, dry_run, now) -> tuple[ActionOutcome, ...]``
  — the orchestrator. Reusable in tests by passing a ``FakeGitHub`` as
  ``client``.

CLI usage:

    python -m rocm_mq process-cycle --fake --repo owner/repo
    python -m rocm_mq process-cycle --dry-run --fake --repo owner/repo
    python -m rocm_mq process-cycle --repo owner/repo   # uses GITHUB_TOKEN

The ``--fake`` flag injects ``tests.gh_fake.FakeGitHub`` so the full cycle
runs against an in-memory substitute with no network access — the DOG-01
"no real GitHub call" property. The import of ``tests.gh_fake`` is guarded
behind the ``--fake`` flag so production code paths never reference test
artefacts.

The ``--dry-run`` flag emits the would-be ``Action`` list to stdout and
returns without calling ``dispatch``. This is Phase 3 forward-compatibility
(WF-07; ROADMAP Phase 3 success criterion 6).

PURE-09 compliance: this module imports ``rocm_mq.snapshot`` /
``rocm_mq.executor`` (both I/O modules) and ``rocm_mq.decision`` /
``rocm_mq.summary`` (both pure). It is intentionally a glue layer; the
PURE-09 lint excludes it from the pure-layer set.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from rocm_mq.decision import decide_cycle, derive_snapshot
from rocm_mq.executor import dispatch
from rocm_mq.snapshot import build_snapshot
from rocm_mq.state import (
    Action,
    ActionOutcome,
    CycleRenderContext,
    MergeQueueConfig,
    PartialPRState,
    PRState,
)
from rocm_mq.summary import render_cycle_summary

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# process_cycle — the orchestrator (testable, no I/O concerns of its own)
# ---------------------------------------------------------------------------


def process_cycle(
    *,
    client: Any,
    config: MergeQueueConfig,
    owner: str,
    repo: str,
    dry_run: bool,
    now: datetime,
) -> tuple[ActionOutcome, ...]:
    """Run one merge-queue processor cycle end-to-end.

    Steps (RFC §4.9):
      1. ``build_snapshot(client, config, owner=owner, repo=repo)`` — I/O.
      2. ``derive_snapshot(raw, config, now)`` — pure. Returns
         ``(Snapshot, tuple[Defer, ...])``. The pre-defers (PRs whose timeline
         events are still lagging, or whose mq:queued label was applied by a
         non-App actor) are surfaced as Defer actions at the front of the
         action list.
      3. ``decide_cycle(snapshot, config, now)`` — pure. Returns
         ``list[Action]``.
      4. If ``dry_run``: print each action to stdout and return empty
         outcomes. No dispatch.
      5. Otherwise: ``dispatch(action, client=..., config=..., owner=...,
         repo=...)`` per action. Collect outcomes.
      6. Render the cycle summary and append it to ``$GITHUB_STEP_SUMMARY``
         if that env var is set (it is set automatically inside GHA runners).
      7. Return the outcomes tuple.

    The ``now`` argument is the single cycle-scope timestamp; it is threaded
    through ``derive_snapshot`` and ``decide_cycle`` (PATTERNS.md "now
    threading"). This module is the only place in ``rocm_mq`` that obtains
    ``now`` from ``datetime.now(tz=UTC)`` (in ``main()`` below).

    Args:
        client: GitHubClient (real) or FakeGitHub (tests). Both expose the
            same ``.rest.*`` surface.
        config: Validated MergeQueueConfig.
        owner: GitHub repository owner login.
        repo: GitHub repository name.
        dry_run: When True, skip dispatch and only print actions.
        now: Cycle-scope timestamp (tz-aware UTC).

    Returns:
        Tuple of ``ActionOutcome`` — one per dispatched action (or empty
        when ``dry_run=True``).
    """
    # Step 1: I/O — read the raw snapshot.
    raw = build_snapshot(client, config, owner=owner, repo=repo)

    # Step 2: pure — derive into PRStates + pre-defers.
    snapshot, pre_defers = derive_snapshot(raw, config, now)

    # Step 3: pure — decide actions.
    decided: list[Action] = decide_cycle(snapshot, config, now)

    # Merge pre-defers (already Defer instances) with the decided action list.
    # Pre-defers go FIRST so the rendered cycle-summary reflects every PR the
    # cycle saw (derived + deferred).
    actions: list[Action] = [*pre_defers, *decided]

    # Step 4: dry-run short-circuit.
    if dry_run:
        for action in actions:
            print(f"[dry-run] Would execute: {_describe_action(action)}")
        return ()

    # Step 5: I/O — dispatch each action.
    outcomes: list[ActionOutcome] = []
    for action in actions:
        outcome = dispatch(
            action,
            client=client,
            config=config,
            owner=owner,
            repo=repo,
        )
        outcomes.append(outcome)

    # Step 6: render summary + write to $GITHUB_STEP_SUMMARY.
    cycle_completed_at = datetime.now(tz=UTC)
    queue_depths = _compute_queue_depths(snapshot, config)
    render_ctx = CycleRenderContext(
        cycle_started_at=now,
        cycle_completed_at=cycle_completed_at,
        cycle_run_url=os.environ.get("GITHUB_SERVER_URL") or None,
        queue_depths=queue_depths,
    )
    summary = render_cycle_summary(
        snapshot, tuple(actions), tuple(outcomes), render_ctx
    )
    step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary_path:
        # Append (GHA convention: multiple steps may append).
        with open(step_summary_path, "a", encoding="utf-8") as fh:
            fh.write(summary)
            if not summary.endswith("\n"):
                fh.write("\n")

    return tuple(outcomes)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _describe_action(action: Action) -> str:
    """Return a short human-readable description for dry-run output.

    Intentionally simple (no exhaustive ``match``) — the dry-run surface is
    informational, not contract. Action's repr already includes the variant
    name and PR number.
    """
    pr_obj = getattr(action, "pr", None)
    pr_number: int | str = (
        pr_obj.number if isinstance(pr_obj, (PRState, PartialPRState)) else "?"
    )
    return f"{type(action).__name__}(pr=#{pr_number})"


def _compute_queue_depths(
    snapshot: Any, config: MergeQueueConfig
) -> tuple[tuple[str, int], ...]:
    """Build the per-queue depth tuple for ``CycleRenderContext``.

    Depth = total number of PRs in the snapshot belonging to that queue
    (membership comes from ``pr.queues``, which is derived from labels).
    """
    depths: list[tuple[str, int]] = []
    for queue in config.all_queues:
        count = sum(1 for pr in snapshot.prs if queue in pr.queues)
        depths.append((queue, count))
    return tuple(depths)


def _build_fake_client() -> Any:
    """Construct a ``FakeGitHub`` instance for ``--fake`` mode.

    The import is guarded behind this helper so production code paths never
    reference ``tests.gh_fake`` — only the ``--fake`` flag triggers the
    import. Tests can also monkeypatch this helper to inject a pre-seeded
    fake (see test_main__dry_run_fake__no_state_mutation).
    """
    # Conditional import — only happens when --fake is set.
    from tests.gh_fake import FakeGitHub, FakeRepoState

    return FakeGitHub(FakeRepoState())


# ---------------------------------------------------------------------------
# main — argparse + dispatch
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the argparse Namespace.

    The CLI shape is ``rocm-mq process-cycle [--fake] [--dry-run] [--repo OWNER/REPO]``.
    The ``process-cycle`` subcommand is a positional placeholder — Phase 3
    will add ``handle`` and ``audit`` subcommands; for Phase 2 it is the
    only valid value.
    """
    parser = argparse.ArgumentParser(
        prog="rocm-mq",
        description=(
            "Federated Merge Queue processor — one cycle of "
            "read → derive → decide → execute."
        ),
    )
    parser.add_argument(
        "subcommand",
        choices=["process-cycle"],
        help="Subcommand to run (Phase 2 only supports process-cycle).",
    )
    parser.add_argument(
        "--fake",
        action="store_true",
        help=(
            "Use the in-memory FakeGitHub substitute instead of the real "
            "GitHub API. Implies no network access (DOG-01)."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Emit the would-be Action list to stdout and exit without "
            "calling dispatch (Phase 3 WF-07 forward compatibility)."
        ),
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help=(
            "GitHub repository in OWNER/REPO form. Defaults to "
            "$GITHUB_REPOSITORY (set automatically in GHA runners)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns process exit code.

    Construction order:
      1. Parse args.
      2. Validate ``--repo`` parses as ``owner/repo``.
      3. Build client (FakeGitHub if --fake, else GitHubClient(GITHUB_TOKEN)).
      4. Build config (Phase 2: canonical_merge_queue_config — Phase 3 will
         load from PATH_TO_QUEUES yaml). For now we always use the canonical
         config; --fake tests inject their own via process_cycle directly.
         For real runs we still use canonical to keep the cycle runnable
         end-to-end (Phase 3 wires the yaml loader).
      5. Compute ``now = datetime.now(tz=UTC)`` — the ONLY datetime.now()
         call at cycle scope in the codebase.
      6. Call ``process_cycle(...)``.
      7. Inspect outcomes; return non-zero if any outcome's success is False.

    Errors raised by argparse (e.g., ``--help``) propagate as ``SystemExit``;
    the caller handles them.
    """
    args = _parse_args(argv)

    # Validate --repo.
    if "/" not in args.repo or args.repo.count("/") != 1:
        print(
            f"error: --repo must be in OWNER/REPO form (got {args.repo!r}); "
            "either pass --repo or set $GITHUB_REPOSITORY.",
            file=sys.stderr,
        )
        return 2
    owner, repo = args.repo.split("/", 1)
    if not owner or not repo:
        print(
            f"error: --repo must be non-empty OWNER/REPO (got {args.repo!r}).",
            file=sys.stderr,
        )
        return 2

    # Build the client.
    client: Any
    if args.fake:
        client = _build_fake_client()
    else:
        token = os.environ.get("GITHUB_TOKEN", "")
        if not token:
            print(
                "error: GITHUB_TOKEN environment variable is not set; pass "
                "--fake for offline testing or mint a token via "
                "actions/create-github-app-token in the workflow.",
                file=sys.stderr,
            )
            return 2
        # Import lazily so test code paths that never instantiate a real
        # GitHubClient do not require githubkit auth machinery initialised.
        from rocm_mq.gh import GitHubClient as _GitHubClient

        client = _GitHubClient(token=token)

    # Build the config. Phase 2: canonical hardcoded config; Phase 4 will
    # load PATH_TO_QUEUES yaml and validate. For --fake runs this is the
    # same config the tests use (canonical_merge_queue_config).
    config = _build_default_config()

    # Cycle-scope `now` — the ONLY datetime.now(tz=UTC) call in rocm_mq.
    now = datetime.now(tz=UTC)

    try:
        outcomes = process_cycle(
            client=client,
            config=config,
            owner=owner,
            repo=repo,
            dry_run=args.dry_run,
            now=now,
        )
    except Exception as exc:  # pragma: no cover  (orchestrator catch-all)
        # Preserve the traceback — operators debugging a production cycle
        # failure need module:line attribution, not just repr(exc) (WR-04).
        print(f"error: process_cycle raised: {exc!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1

    # Exit non-zero if any outcome failed. CorruptSquashError already arrives
    # as ActionOutcome(success=False, error_message="<exc>") from
    # _handle_squash, so this captures the Pitfall 8 alert path.
    failures = [o for o in outcomes if not o.success]
    if failures:
        print(
            f"error: {len(failures)} action(s) failed this cycle:",
            file=sys.stderr,
        )
        for o in failures:
            print(
                f"  - {type(o.action).__name__}: {o.error_message}",
                file=sys.stderr,
            )
        return 1

    return 0


def _build_default_config() -> MergeQueueConfig:
    """Default MergeQueueConfig for Phase 2 CLI runs.

    Phase 4 (PATH_TO_QUEUES loader) will replace this with a yaml-driven
    factory. For Phase 2 we use the canonical config so --fake runs
    end-to-end against the same shape the tests exercise.

    Note: in Phase 2 tests we import ``canonical_merge_queue_config`` from
    ``tests.conftest`` and pass it directly to ``process_cycle`` — this
    factory is only used by the ``main()`` real-token path.
    """
    # Import lazily so we never depend on tests/ at import time.
    from rocm_mq.state import AppIdentity

    return MergeQueueConfig(
        all_queues=("hipdnn",),
        path_to_queues=(),
        app_identity=AppIdentity(slug="rocm-mq", app_id=0, bot_user_id=0),
    )


__all__ = ["main", "process_cycle"]
