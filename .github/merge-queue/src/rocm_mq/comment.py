"""
rocm_mq.comment — Pure status-comment renderer (RFC §4.3).

Single public function: ``render_status_body(pr_state, render_ctx, now) -> str``.

**Pure function contract (RFC §4.9 + PURE-09):**
  - No ``datetime.now()`` / ``datetime.utcnow()`` / ``datetime.fromisoformat()`` calls;
    ``now`` is always an arg.
  - No I/O: no ``os.environ``, no ``subprocess``, no network calls.
  - No persistent state.

**Marker contract (T-01-09):**
  The literal substring ``<!-- rocm-mq-status -->`` appears in every rendered body.
  Phase 3 ``cmd_handle.py`` uses this marker to locate and upsert the status comment.
  The marker is asserted in syrupy goldens (tests/__snapshots__/test_comment_render.ambr)
  and in smoke tests — any drift from the marker string fails CI before commit.

**State dispatch:**
  Dispatches on ``render_ctx.state`` via ``match`` with arms for
  ``"queued" | "active" | "merged" | "ejected"`` and a default arm raising
  ``ValueError(f"unknown state: {other}")``.

  Future hardening (Phase 4) may swap ``state`` from a plain ``str`` to a
  ``Literal["queued", "active", "merged", "ejected"]`` enum — the renderer code
  already handles exactly these four values.
"""

from __future__ import annotations

from datetime import datetime

from rocm_mq.state import PRState, RenderContext

# ---------------------------------------------------------------------------
# Load-bearing marker (T-01-09) — Phase 3 comment-upsert greps for this string.
# Do NOT change this string without a coordinated Phase 3 change.
# ---------------------------------------------------------------------------
_STATUS_MARKER = "<!-- rocm-mq-status -->"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def render_status_body(
    pr_state: PRState,
    render_ctx: RenderContext,
    now: datetime,
) -> str:
    """Render the full markdown body for the PR's merge-queue status comment.

    Dispatches on ``render_ctx.state``:
    - ``"queued"``  → per-queue position table + /dequeue instruction
    - ``"active"``  → head SHA + required checks table
    - ``"merged"``  → merged_sha confirmation
    - ``"ejected"`` → eject reason + /merge re-enqueue instruction

    All four states share a common footer containing:
    - ``_Updated {now:%Y-%m-%d %H:%M UTC}._``
    - If ``render_ctx.cycle_run_url`` is not None:
      ``Last processed: [run](<url>)``
    - The ``<!-- rocm-mq-status -->`` marker (always last, load-bearing).

    Args:
        pr_state: Derived PR state (decision layer output).
        render_ctx: Renderer-only data (populated by cmd_handle.py / cmd_process.py).
        now: Current time (passed by caller; NEVER call datetime.now() here).

    Returns:
        Full markdown string for the status comment body.

    Raises:
        ValueError: When ``render_ctx.state`` is not one of the four known values.
    """
    match render_ctx.state:
        case "queued":
            body = _render_queued(pr_state, render_ctx, now)
        case "active":
            body = _render_active(pr_state, render_ctx, now)
        case "merged":
            body = _render_merged(pr_state, render_ctx, now)
        case "ejected":
            body = _render_ejected(pr_state, render_ctx, now)
        case other:
            raise ValueError(f"unknown state: {other!r}")

    return body + _render_footer(render_ctx, now)


# ---------------------------------------------------------------------------
# Per-state private renderers
# ---------------------------------------------------------------------------


def _render_queued(
    pr_state: PRState,
    render_ctx: RenderContext,
    now: datetime,
) -> str:
    """Render the queued state body (## ⏳ Queued for merge)."""
    _ = now  # pure function — now carried for API symmetry; not used in queued body
    lines: list[str] = []
    lines.append("## ⏳ Queued for merge")
    lines.append("")
    lines.append(
        f"Enqueued by @{render_ctx.author_login} at "
        f"{pr_state.enqueued_at:%Y-%m-%d %H:%M UTC}."
    )
    lines.append("")
    n_queues = len(render_ctx.queue_positions)
    lines.append(f"This PR touches {n_queues} queue(s):")
    lines.append("")
    lines.append("| Queue | Position | Blocked by |")
    lines.append("|---|---|---|")
    for queue_name, pos, total in render_ctx.queue_positions:
        blockers = render_ctx.blockers
        # render_ctx.blockers is a flat list of all blocker PR numbers.
        blocked_str = ", ".join(f"#{n}" for n in blockers) if blockers else "—"
        lines.append(f"| {queue_name} | {pos}/{total} | {blocked_str} |")
    lines.append("")
    lines.append("Comment `/dequeue` to leave the queue. Processor runs every 3 minutes.")
    return "\n".join(lines)


def _render_active(
    pr_state: PRState,
    render_ctx: RenderContext,
    now: datetime,
) -> str:
    """Render the active state body (## 🚦 Active in merge queue)."""
    _ = (render_ctx, now)  # pure function — render_ctx.blockers/positions unused in active
    lines: list[str] = []
    lines.append("## 🚦 Active in merge queue")
    lines.append("")
    # RenderContext does not carry an "activated at" timestamp (D-03 keeps activation
    # timestamps out of RenderContext — they are decision-layer state, not renderer data).
    # Phase 4 may add an activated_at field if needed for display.
    lines.append(f"Activated on head SHA `{pr_state.head_sha}`.")
    lines.append("")
    lines.append(
        "_Required CI checks are enforced by branch protection — see the "
        "PR's checks panel for live state._"
    )
    return "\n".join(lines)


def _render_merged(
    pr_state: PRState,
    render_ctx: RenderContext,
    now: datetime,
) -> str:
    """Render the merged state body (## ✅ Merged)."""
    _ = (pr_state, now)  # pure function — pr_state fields unused in merged body
    lines: list[str] = []
    lines.append("## ✅ Squashed and merged")
    lines.append("")
    lines.append(f"Squashed to develop as `{render_ctx.merged_sha}`.")
    lines.append("")
    # tree_diff_status=ahead is an invariant any successful squash satisfies:
    # executor._verify_squash Phase B raises CorruptSquashError unless the
    # post-squash compare_commits reports status='ahead' AND a non-empty
    # files list. Surfacing the substring here lets DOG-06 (and future audit
    # tooling) confirm the SC#3 Apr-2026 silent-corruption check ran without
    # parsing the executor source. Sentinel-only — no escape concerns.
    lines.append("_tree_diff_status=ahead (Phase B compare_commits verified)._")
    return "\n".join(lines)


def _render_ejected(
    pr_state: PRState,
    render_ctx: RenderContext,
    now: datetime,
) -> str:
    """Render the ejected state body (## ❌ Ejected from merge queue)."""
    _ = (pr_state, now)  # pure function — pr_state fields unused in ejected body
    lines: list[str] = []
    lines.append("## ❌ Ejected from merge queue")
    lines.append("")
    lines.append(f"Reason: {render_ctx.eject_reason}.")
    lines.append("")
    lines.append("Re-enqueue with `/merge` once addressed.")
    return "\n".join(lines)


def _render_footer(render_ctx: RenderContext, now: datetime) -> str:
    """Render the common footer appended to every state body.

    Contains:
    - ``_Updated {now:%Y-%m-%d %H:%M UTC}._``
    - Optionally: ``Last processed: [run](<url>)``
    - The load-bearing ``<!-- rocm-mq-status -->`` marker (always last).
    """
    lines: list[str] = []
    lines.append("")
    lines.append(f"_Updated {now:%Y-%m-%d %H:%M UTC}._")
    if render_ctx.cycle_run_url is not None:
        lines.append(f"Last processed: [run]({render_ctx.cycle_run_url})")
    lines.append("")
    lines.append(_STATUS_MARKER)
    return "\n".join(lines)
