"""
rocm_mq.summary — Pure cycle-summary renderer (RFC §4.6 $GITHUB_STEP_SUMMARY).

Single public function: ``render_cycle_summary(snapshot, actions, outcomes, render_ctx) -> str``.

**Pure function contract (RFC §4.9):**
  - No ``datetime.now()`` / ``datetime.utcnow()`` / ``datetime.fromisoformat()`` calls;
    timestamps are taken from ``render_ctx`` args only.
  - No I/O: no ``os.environ``, no ``subprocess``, no network calls.
  - No persistent state.

**RFC §4.6 four-section layout:**
  1. ``## Queue depth`` — per-queue depth table from ``render_ctx.queue_depths``.
  2. ``## Active PRs`` — PRs with ``mq:active`` label in the snapshot.
  3. ``## Cycle outcomes`` — one line per Action+Outcome pair, dispatched via
     ``match action: ... case _: assert_never(action)`` (exhaustiveness enforced by
     mypy --strict on this module).
  4. ``## Cycle duration`` — formatted ``Mm:Ss`` or ``Hh:Mm``.
"""

from __future__ import annotations

from datetime import datetime
from typing import assert_never

from rocm_mq.state import (
    Action,
    ActionOutcome,
    Activate,
    CycleRenderContext,
    Defer,
    Eject,
    Snapshot,
    Squash,
    UpdateComment,
)

# Active label used to filter snapshot.prs for the "Active PRs" section.
# Matches MergeQueueConfig.active_label default.
_ACTIVE_LABEL = "mq:active"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_cycle_summary(
    snapshot: Snapshot,
    actions: tuple[Action, ...],
    outcomes: tuple[ActionOutcome, ...],
    render_ctx: CycleRenderContext,
) -> str:
    """Render the full markdown for the per-cycle ``$GITHUB_STEP_SUMMARY``.

    Produces four sections in fixed order (RFC §4.6):
    1. ``## Queue depth`` — table from ``render_ctx.queue_depths``.
    2. ``## Active PRs`` — snapshot PRs with ``mq:active`` label.
    3. ``## Cycle outcomes`` — one line per Action+Outcome pair.
    4. ``## Cycle duration`` — ``Mm:Ss`` or ``Hh:Mm`` format.

    Args:
        snapshot: Post-derive PR snapshot (input to decide_cycle).
        actions: Ordered tuple of Actions emitted by decide_cycle.
        outcomes: Ordered tuple of ActionOutcomes, one-to-one with ``actions``.
        render_ctx: Renderer-only cycle-level data.

    Returns:
        Full markdown string for the GitHub Step Summary.

    Raises:
        ValueError: When ``len(actions) != len(outcomes)`` — the executor
            guarantees parity; this is a defensive check.
    """
    if len(actions) != len(outcomes):
        raise ValueError(
            f"actions and outcomes length mismatch: "
            f"{len(actions)} actions vs {len(outcomes)} outcomes"
        )

    sections = [
        _render_queue_depth(render_ctx),
        _render_active_prs(snapshot),
        _render_outcomes(actions, outcomes),
        _render_duration(render_ctx),
    ]
    result = "\n\n".join(sections)

    if render_ctx.cycle_run_url is not None:
        result += f"\n\nCycle run: <{render_ctx.cycle_run_url}>"

    return result


# ---------------------------------------------------------------------------
# Section renderers
# ---------------------------------------------------------------------------


def _render_queue_depth(render_ctx: CycleRenderContext) -> str:
    """Section 1: Per-queue depth table.

    Renders ``render_ctx.queue_depths`` (``tuple[tuple[str, int], ...]``) —
    one row per queue with the total depth (queued + active) for that queue.
    """
    lines: list[str] = ["## Queue depth", "", "| Queue | Depth |", "|---|---|"]
    for queue_name, depth in render_ctx.queue_depths:
        lines.append(f"| {queue_name} | {depth} |")
    return "\n".join(lines)


def _render_active_prs(snapshot: Snapshot) -> str:
    """Section 2: Active PRs in the snapshot.

    Filters ``snapshot.prs`` to those with ``mq:active`` in labels.
    """
    active = [pr for pr in snapshot.prs if _ACTIVE_LABEL in pr.labels]
    lines: list[str] = ["## Active PRs", ""]
    if not active:
        lines.append("_None_")
    else:
        for pr in active:
            short_sha = pr.head_sha[:7]
            enqueued_str = pr.enqueued_at.strftime("%Y-%m-%d %H:%M UTC")
            lines.append(f"- #{pr.number} (head `{short_sha}`, enqueued at {enqueued_str})")
    return "\n".join(lines)


def _render_outcomes(
    actions: tuple[Action, ...],
    outcomes: tuple[ActionOutcome, ...],
) -> str:
    """Section 3: Cycle outcomes, one line per action+outcome pair.

    Dispatches on the Action variant with ``match action``.
    ``case _: assert_never(action)`` enforces exhaustiveness at mypy --strict
    time.

    A failed outcome appends ``— FAILED: {error_message}`` to the action line.
    When no actions occurred, renders ``_No actions this cycle._``.
    """
    lines: list[str] = ["## Cycle outcomes", ""]
    if not actions:
        lines.append("_No actions this cycle._")
        return "\n".join(lines)

    for action, outcome in zip(actions, outcomes, strict=True):
        match action:
            case Activate(pr=pr):
                line = f"- ✨ Activate #{pr.number}"
            case Squash(pr=pr):
                line = f"- ✅ Squash #{pr.number}"
            case Eject(pr=pr, reason=reason):
                line = f"- ❌ Eject #{pr.number}: {reason}"
            case UpdateComment(pr=pr):
                line = f"- 💬 Update comment on #{pr.number}"
            case Defer(pr=pr, reason=reason):
                pr_number = pr.number  # works for both PRState and PartialPRState
                line = f"- ⏸ Defer #{pr_number}: {reason}"
            case _:
                # Exhaustive match — add new Action variants here.
                assert_never(action)

        if not outcome.success:
            line += f" — FAILED: {outcome.error_message}"
        lines.append(line)

    return "\n".join(lines)


def _render_duration(render_ctx: CycleRenderContext) -> str:
    """Section 4: Cycle duration in ``Mm:Ss`` or ``Hh:Mm`` format."""
    duration_str = _format_duration(
        render_ctx.cycle_started_at, render_ctx.cycle_completed_at
    )
    return f"## Cycle duration\n\n{duration_str}"


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _format_duration(start: datetime, end: datetime) -> str:
    """Format the duration between two datetimes as ``Mm:Ss`` or ``Hh:Mm``.

    - Under 1 hour: ``{m}m:{s:02d}s`` (e.g., ``0m:30s``, ``2m:05s``).
    - 1 hour or more: ``{h}h:{m:02d}m`` (e.g., ``1h:03m``).
    """
    total_seconds = int((end - start).total_seconds())
    if total_seconds < 3600:
        m, s = divmod(total_seconds, 60)
        return f"{m}m:{s:02d}s"
    total_minutes, _ = divmod(total_seconds, 60)
    h, m = divmod(total_minutes, 60)
    return f"{h}h:{m:02d}m"
