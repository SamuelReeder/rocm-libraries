"""
rocm_mq.cmd_process — Process-cycle orchestrator and CLI entrypoint.

Orchestrates the full RFC §4.6 read → derive → decide → execute loop:

    build_snapshot  →  derive_snapshot  →  decide_cycle  →  dispatch
        (I/O)           (pure)            (pure)          (I/O)

This module is the glue layer between the I/O adapters (``snapshot.py``,
``executor.py``, ``gh.py``) and the pure decision layer. It is the ONLY
module in ``rocm_mq`` that calls ``datetime.now(tz=UTC)`` at cycle scope
(PATTERNS.md "now threading" rule): the single ``now`` value is computed in
``main()`` and threaded through to ``derive_snapshot`` and ``decide_cycle``
so all per-cycle timestamps are consistent and deterministic in tests.

Public surface:
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
    python -m rocm_mq handle --repo owner/repo --event-path $GITHUB_EVENT_PATH
    python -m rocm_mq audit
    python -m rocm_mq preflight --repo owner/repo

The ``--fake`` flag injects ``tests.gh_fake.FakeGitHub`` so the full cycle
runs against an in-memory substitute with no network access — the DOG-01
"no real GitHub call" property. The import of ``tests.gh_fake`` is guarded
behind the ``--fake`` flag so production code paths never reference test
artefacts.

The ``--dry-run`` flag emits the would-be ``Action`` list to stdout and
returns without calling ``dispatch``.

PURE-09 compliance: this module imports ``rocm_mq.snapshot`` /
``rocm_mq.executor`` (both I/O modules) and ``rocm_mq.decision`` /
``rocm_mq.summary`` (both pure). It is intentionally a glue layer; the
PURE-09 lint excludes it from the pure-layer set.

``_parse_args`` uses ``add_subparsers(dest="subcommand", required=True)``
with four subparsers (``process-cycle``, ``handle``, ``audit``,
``preflight``); each registers a per-subcommand ``set_defaults(func=run_*)``
callable so ``main()`` reduces to ``args.func(args)`` wrapped in the
existing traceback-printing try/except. ``handle`` dispatches into
``rocm_mq.cmd_handle.main``. ``preflight`` dispatches into
``rocm_mq.preflight.main``. ``audit`` is a stub pending RFC §4.3.1
tamper-matrix implementation.
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from rocm_mq.decision import decide_cycle, derive_snapshot
from rocm_mq.executor import dispatch
from rocm_mq.snapshot import build_snapshot
from rocm_mq.state import (
    Action,
    ActionOutcome,
    AppIdentity,
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
      6. Render the cycle summary and write it to TWO sinks
         (RESEARCH.md Area #11):
           - append to ``$GITHUB_STEP_SUMMARY`` (if set — automatic in
             GHA runners) for the per-run Actions UI summary panel;
           - OVERWRITE the file at ``$MQ_CYCLE_SUMMARY_PATH`` (default
             ``cycle-summary.md``) so ``actions/upload-artifact`` can
             publish it for downstream dogfood drivers to fetch via the
             GitHub artifacts API.
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

    # Step 6: render summary + write to two sinks.
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

    # Second sink: cycle-summary.md file. This file is exposed as an
    # actions/upload-artifact upload so dogfood drivers can download a
    # structured copy of the cycle summary via the GitHub artifacts API
    # (raw run-logs zip is not cleanly parseable). Unlike
    # $GITHUB_STEP_SUMMARY (appended), this path is OVERWRITTEN per cycle
    # — each cycle's summary stands alone.
    # Default path matches the upload-artifact step's `path:` input in
    # .github/workflows/mq-processor.yml.
    cycle_summary_path = os.environ.get(
        "MQ_CYCLE_SUMMARY_PATH", "cycle-summary.md"
    )
    cycle_summary_file = Path(cycle_summary_path)
    # Create the parent dir if missing — the workflow's working-directory
    # is .github/merge-queue, so the default cycle-summary.md lands there
    # without mkdir, but explicit MQ_CYCLE_SUMMARY_PATH overrides may
    # point into a nested directory that does not yet exist.
    cycle_summary_file.parent.mkdir(parents=True, exist_ok=True)
    payload = summary if summary.endswith("\n") else summary + "\n"
    cycle_summary_file.write_text(payload, encoding="utf-8")

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

    ``--fake`` mode requires the ``tests/`` directory to be on the Python
    import path. The wheel build (``pyproject.toml`` ``packages =
    ["src/rocm_mq"]``) ships only the package source — running ``--fake``
    against a wheel-installed copy produces a ``ModuleNotFoundError``.
    Catch that and emit an actionable message rather than a bare stack
    trace (preserves module:line attribution in tracebacks).
    """
    # Conditional import — only happens when --fake is set.
    try:
        from tests.gh_fake import FakeGitHub, FakeRepoState
    except ModuleNotFoundError as exc:
        print(
            "error: --fake mode requires the tests/ directory on the Python "
            "import path. The wheel build excludes tests/ (pyproject.toml "
            "`packages = [\"src/rocm_mq\"]`), so --fake only works from an "
            "editable install (`pip install -e .[dev]`). Re-install in "
            "editable mode or drop --fake to use the real GitHub API.",
            file=sys.stderr,
        )
        raise SystemExit(2) from exc

    return FakeGitHub(FakeRepoState())


# ---------------------------------------------------------------------------
# main — argparse + dispatch
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Build the argparse Namespace using a subparser tree.

    Uses ``add_subparsers(dest="subcommand", required=True)`` with four
    registered subcommands, each carrying its own isolated flag namespace
    and a ``set_defaults(func=...)`` that ``main()`` invokes as
    ``args.func(args)``.

    Subcommands:
      - ``process-cycle``: run one processor cycle (RFC §4.6). Flags:
        ``--fake``, ``--dry-run``, ``--repo OWNER/REPO``.
      - ``handle``: dispatch a /merge or /dequeue issue_comment event.
        Flags: ``--repo OWNER/REPO``,
        ``--event-path PATH`` (defaults to ``$GITHUB_EVENT_PATH``).
      - ``audit``: RFC §4.3.1 tamper-matrix audit (not yet implemented).
        No flags.
      - ``preflight``: workflow pre-flight invariant check. Flags:
        ``--repo OWNER/REPO``.

    Backward-compat note: existing internal test calls of the form
    ``main(["process-cycle", ...])`` continue to work because
    ``process-cycle`` is a subparser of the same literal name. The
    ``mq-test.yml`` workflow does not invoke ``cmd_process`` directly
    (runs ``pytest``), so no external CI consumer breaks.
    """
    parser = argparse.ArgumentParser(
        prog="rocm-mq",
        description=(
            "Federated Merge Queue — controller CLI for the processor "
            "cycle (process-cycle), the command handler (handle), the "
            "tamper-audit job (audit), and the workflow pre-flight check "
            "(preflight)."
        ),
    )
    sub = parser.add_subparsers(dest="subcommand", required=True)

    # process-cycle — the processor cycle orchestrator (RFC §4.6).
    p_cycle = sub.add_parser(
        "process-cycle",
        help="Run one merge-queue processor cycle (RFC §4.6).",
    )
    p_cycle.add_argument(
        "--fake",
        action="store_true",
        help=(
            "Use the in-memory FakeGitHub substitute instead of the real "
            "GitHub API. Implies no network access (DOG-01)."
        ),
    )
    p_cycle.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Emit the would-be Action list to stdout and exit without "
            "calling dispatch."
        ),
    )
    p_cycle.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help=(
            "GitHub repository in OWNER/REPO form. Defaults to "
            "$GITHUB_REPOSITORY (set automatically in GHA runners)."
        ),
    )
    p_cycle.set_defaults(func=run_process_cycle)

    # handle — dispatches into rocm_mq.cmd_handle.
    p_handle = sub.add_parser(
        "handle",
        help="Handle a /merge or /dequeue issue_comment event.",
    )
    p_handle.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="GitHub repository in OWNER/REPO form.",
    )
    p_handle.add_argument(
        "--event-path",
        default=os.environ.get("GITHUB_EVENT_PATH", ""),
        help=(
            "Path to the GHA event JSON (issue_comment payload). Defaults "
            "to $GITHUB_EVENT_PATH (set automatically in GHA runners)."
        ),
    )
    p_handle.set_defaults(func=run_handle)

    # audit — RFC §4.3.1 tamper-matrix audit (not yet implemented).
    p_audit = sub.add_parser(
        "audit",
        help="RFC §4.3.1 tamper-matrix audit (not yet implemented).",
    )
    p_audit.set_defaults(func=run_audit)

    # preflight — dispatches into rocm_mq.preflight.
    p_preflight = sub.add_parser(
        "preflight",
        help="Workflow pre-flight invariant check.",
    )
    p_preflight.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="GitHub repository in OWNER/REPO form.",
    )
    p_preflight.set_defaults(func=run_preflight)

    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# Per-subcommand entrypoints — registered via ``set_defaults(func=...)``.
# ---------------------------------------------------------------------------


def run_process_cycle(args: argparse.Namespace) -> int:
    """Run the processor cycle end-to-end.

    Construction order:
      1. Validate ``--repo`` parses as ``owner/repo``.
      2. Build client (FakeGitHub if --fake, else GitHubClient(GITHUB_TOKEN)).
      3. Resolve the App's canonical identity via ``resolve_app_identity``
         (sentinel zeros would silently reject every legitimate
         status/timeline event).
      4. Build the MergeQueueConfig (from PATH_TO_QUEUES yaml in real-token
         mode; from a hardcoded default in --fake mode).
      5. Compute ``now = datetime.now(tz=UTC)`` — the ONLY datetime.now()
         call at cycle scope in the codebase.
      6. Call ``process_cycle(...)``.
      7. Inspect outcomes; return non-zero if any outcome's success is False.

    Returns process exit code (0 on success; 1 on any failed outcome or
    orchestrator exception; 2 on argparse-shape errors).
    """
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

    # Build the config.
    #
    # --fake mode: hardcoded canonical config (`hipdnn` only), matching the
    # shape tests use. resolve_app_identity is called via the FakeGitHub's
    # stubbed apps/users namespaces.
    #
    # Real-token mode: load path_to_queues.yml from develop via the Contents
    # API and translate to MergeQueueConfig with all opted-in queues. The
    # processor sees every queue the YAML lists. resolve_app_identity is
    # called inside build_config_from_develop.
    #
    # Schema-graph validation (every queue named in a path entry must exist
    # in queues:, upstream/downstream closure) is handled by
    # mq-config-validate.yml; this loader is non-validating beyond the
    # "root is a mapping" sanity check.
    config: MergeQueueConfig
    if args.fake:
        from rocm_mq.gh import resolve_app_identity

        app_identity = resolve_app_identity(client)
        config = _build_default_config(app_identity=app_identity)
    else:
        from rocm_mq.config import build_config_from_develop

        config = build_config_from_develop(client, owner, repo)

    # Cycle-scope `now` — the ONLY datetime.now(tz=UTC) call in rocm_mq.
    now = datetime.now(tz=UTC)

    outcomes = process_cycle(
        client=client,
        config=config,
        owner=owner,
        repo=repo,
        dry_run=args.dry_run,
        now=now,
    )

    # Exit non-zero if any outcome failed. CorruptSquashError already arrives
    # as ActionOutcome(success=False, error_message="<exc>") from
    # _handle_squash (catches the Eject case from _handle_squash).
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


def run_handle(args: argparse.Namespace) -> int:
    """Dispatch the ``handle`` subcommand into ``rocm_mq.cmd_handle.main``.

    Re-serializes the parsed ``--repo`` / ``--event-path`` flags so
    ``cmd_handle.main`` re-parses them. Re-parsing keeps both entrypoints
    (``python -m rocm_mq handle ...`` and a direct
    ``python -m rocm_mq.cmd_handle ...`` invocation if one is ever added)
    independently usable with the same flag surface — mirrors the pattern
    ``run_preflight`` uses for ``rocm_mq.preflight.main``.

    Exit codes preserved verbatim from ``cmd_handle.main``:
      - 0 — every "handled" outcome (successful enqueue, idempotent
        short-circuit, every rejection path, every event-skip path).
      - 1 — uncaught exception (traceback printed to stderr).
      - 2 — usage error (malformed ``--repo`` or missing ``GITHUB_TOKEN``).
    """
    # Deferred import (matches the ``run_preflight`` and ``_build_fake_client``
    # pattern) — keeps cmd_process importable from contexts that never call
    # the handler and avoids hard-wiring a runtime dependency the
    # ``process-cycle`` subcommand does not need.
    from rocm_mq.cmd_handle import main as _handle_main

    return _handle_main(
        [f"--repo={args.repo}", f"--event-path={args.event_path}"]
    )


def run_audit(args: argparse.Namespace) -> int:
    """RFC §4.3.1 tamper-matrix audit (not yet implemented).

    Returns 0 cleanly so mq-handler.yml's audit job slot can be authored
    and exercised end-to-end ahead of the implementation, but prints a
    structured stderr note so operators reading the workflow run log do not
    mistake the no-op for "audit logic ran".
    """
    print(
        "audit: not yet implemented; RFC §4.3.1 tamper-matrix logic "
        "(label tamper, status tamper, comment tamper) pending.",
        file=sys.stderr,
    )
    return 0


def run_preflight(args: argparse.Namespace) -> int:
    """Dispatch into ``rocm_mq.preflight.main``.

    Re-serializes the parsed ``--repo`` flag and hands off to
    ``preflight.main``. The pre-flight CLI re-parses argv so the two
    entrypoints (``python -m rocm_mq preflight`` and the workflow's
    ``python -m rocm_mq.preflight`` invocation when called directly)
    stay independently usable with the same flag surface.

    Exit codes preserved verbatim from ``preflight.main``:
      - 0 — all checks pass
      - 1 — Check 1 (default-branch) or Check 3 (path_to_queues loadable)
        failed
      - 2 — usage error (missing/malformed ``--repo`` or missing
        ``GITHUB_TOKEN``)
    """
    # Import here (not at module top) to avoid an import cycle should
    # rocm_mq.preflight ever grow a dependency on cmd_process; the
    # current code shape has no cycle, but the deferred import is a cheap
    # invariant guard and matches the pattern cmd_process uses for the
    # tests.gh_fake import in _build_fake_client.
    from rocm_mq.preflight import main as _preflight_main

    return _preflight_main([f"--repo={args.repo}"])


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint — parses argv and dispatches to the registered subcommand.

    Reduces to:

        args = _parse_args(argv)
        return args.func(args)

    wrapped in a shared try/except that prints repr + traceback to stderr
    on any uncaught exception. This unifies the failure mode across all
    four subparser handlers — any unhandled exception surfaces with
    structured stderr including module:line attribution in the traceback.

    Errors raised by argparse (e.g., ``--help``, missing required
    subcommand) propagate as ``SystemExit``; the caller handles them.
    """
    args = _parse_args(argv)

    try:
        return int(args.func(args))
    except Exception as exc:
        # Preserve the traceback — operators debugging a production
        # failure need module:line attribution, not just repr(exc).
        print(f"error: {type(exc).__name__}: {exc!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


def _build_default_config(*, app_identity: AppIdentity) -> MergeQueueConfig:
    """Build the hardcoded default MergeQueueConfig used in --fake mode.

    Uses the canonical single-queue config (``hipdnn`` only) so --fake
    runs end-to-end against the same shape the tests exercise. In tests,
    ``canonical_merge_queue_config`` from ``tests.conftest`` is passed
    directly to ``process_cycle``; this factory is only used by the
    --fake CLI path.

    Args:
        app_identity: The App's resolved canonical identity (slug + app_id +
            bot_user_id), produced by ``resolve_app_identity(client)`` at
            cycle startup. Sentinel zeros would reject every legitimate
            status/timeline event in the decision layer; the field is
            keyword-only and required to make accidental zero-stubs a
            type error rather than a silent production no-op.
    """
    return MergeQueueConfig(
        all_queues=("hipdnn",),
        path_to_queues=(),
        app_identity=app_identity,
    )


__all__ = [
    "main",
    "process_cycle",
    "run_audit",
    "run_handle",
    "run_preflight",
    "run_process_cycle",
]
