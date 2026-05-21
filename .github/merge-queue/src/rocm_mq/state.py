"""
rocm_mq.state — Frozen-dataclass state model for the federated merge queue.

All dataclasses in this module are ``@dataclass(frozen=True, slots=True)``.
All collections are ``tuple[T, ...]`` or ``frozenset[T]`` — never ``list``, ``set``,
or ``dict`` (Pitfall 10: mutable-default aliasing; RFC §4.9 pure-layer contract).

Two PR dataclass families (D-01):
  Raw*   — mirror the GitHub API response shape; constructed by Phase 2 ``snapshot.py``.
  Derived — ``PRState``, ``Snapshot``; output of ``derive_pr``/``derive_snapshot``.

Design decisions locked in CONTEXT.md (D-01..D-04) and RESEARCH.md.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Raw family — mirror the GitHub API, unfiltered (D-02)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CommitStatusCreator:
    """Creator of a commit status check.

    ``app_slug`` and ``app_id`` are ``None`` when the creator is a User
    (not a GitHub App). Both must be present for ``is_app_identity`` to return True.
    """

    login: str
    type: str  # "Bot" | "User" | "Organization"
    app_slug: str | None  # populated when type == "Bot" and creator is a GitHub App
    app_id: int | None


@dataclass(frozen=True, slots=True)
class CommitStatus:
    """A single commit status on a PR's head SHA.

    Carries full creator metadata so the pure derive can apply BOTH the context
    filter and the creator (App-identity) filter (D-02 — Pitfall 2 family).
    """

    context: str
    state: str  # "pending" | "success" | "failure" | "error"
    creator: CommitStatusCreator
    created_at: datetime


@dataclass(frozen=True, slots=True)
class TimelineActor:
    """Actor on a PR timeline event (e.g., a label-applied event).

    Uses ``user_id`` (the bot's stable numeric user ID, different from ``app_id``)
    for identity checks via ``is_app_identity_actor``.
    """

    login: str
    type: str  # "Bot" | "User" | "Organization"
    user_id: int


@dataclass(frozen=True, slots=True)
class LabelEvent:
    """A label-applied / label-removed timeline event on a PR.

    The ``mq:queued`` label-application events with ``is_app_identity_actor(actor)``
    are the canonical FIFO timestamp source (Claude's Discretion in CONTEXT.md).
    """

    label_name: str
    event: str  # "labeled" | "unlabeled"
    actor: TimelineActor
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RawPRState:
    """Raw GitHub API state for a single PR — unfiltered, unprocessed (D-01, D-02).

    ``head_statuses`` carries EVERY commit status on the current head SHA.
    ``mq_queued_label_events`` carries EVERY ``mq:queued`` label timeline event.
    The pure ``derive_pr`` applies all filtering (context + creator).

    Note: per 03-wr-09 design refactor, required CI check evaluation is
    NOT done by the queue. Branch protection is the single source of truth
    for which checks gate a merge; the executor reads GitHub's merge-API
    response to translate protection-blocked merges into Eject actions.
    """

    number: int
    head_sha: str
    labels: frozenset[str]
    head_statuses: tuple[CommitStatus, ...]
    mq_queued_label_events: tuple[LabelEvent, ...]
    changed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RawSnapshot:
    """A raw snapshot of all relevant PRs fetched from the GitHub API (D-04)."""

    prs: tuple[RawPRState, ...]


# ---------------------------------------------------------------------------
# Derived family — output of ``derive_pr`` / ``derive_snapshot`` (D-01, D-04)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PRState:
    """Derived PR state — contains only fields the decision algorithm reads (D-03).

    Renderer-only fields (``author_login``, PR title, blocker PR numbers, run URL)
    are explicitly absent — they live in ``RenderContext`` (D-03).

    ``enqueued_at`` must be tz-aware; ``is_validly_active`` is computed by
    ``derive_pr`` applying BOTH context and creator filters (D-02).
    """

    number: int
    head_sha: str
    labels: frozenset[str]
    queues: frozenset[str]  # queue names this PR belongs to (from pathmap + labels)
    enqueued_at: datetime  # must be tz-aware; FIFO sort key
    is_validly_active: bool


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Post-derive flat tuple of derived PR states — input to ``decide_cycle`` (D-04).

    ``now`` is NOT embedded here; it is passed as a separate arg to ``decide_cycle``
    so Hypothesis tests can vary time independently of PR fixtures (D-04 rationale).
    """

    prs: tuple[PRState, ...]


# ---------------------------------------------------------------------------
# Q1 / Q4 resolution sum types — locked in CONTEXT.md open questions
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeferredPR:
    """Second arm of ``derive_pr``'s return type (Q1 resolution).

    Returned when raw derivation cannot produce a full ``PRState`` — e.g., when
    no qualifying ``mq:queued`` label-application event exists (Pitfall 4:
    timeline eventual consistency).
    """

    number: int
    reason: str


@dataclass(frozen=True, slots=True)
class PartialPRState:
    """Minimal PR record used as the second arm of ``Defer.pr`` (Q4 resolution).

    Carried when the executor emitted a ``Defer`` action for a PR that could not
    be fully derived. Exported from ``state.py`` so Phase 2 / Phase 4 callers can
    type-check ``Defer.pr`` as ``PRState | PartialPRState``.
    """

    number: int
    head_sha: str
    labels: frozenset[str]


# ---------------------------------------------------------------------------
# Action union — PEP 604, variant dataclasses + assert_never exhaustiveness
# (CONTEXT.md Claude's Discretion + Pattern 2 in RESEARCH.md)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Activate:
    """Activate a PR: post the ``merge-queue/active`` commit status."""

    pr: PRState


@dataclass(frozen=True, slots=True)
class Squash:
    """Squash-merge a PR that is head-of-all-queues and all checks pass."""

    pr: PRState


@dataclass(frozen=True, slots=True)
class Eject:
    """Eject a PR from the merge queue with a human-readable reason."""

    pr: PRState
    reason: str


@dataclass(frozen=True, slots=True)
class UpdateComment:
    """Update the PR's merge-queue status comment with a new body."""

    pr: PRState
    new_body: str


@dataclass(frozen=True, slots=True)
class Defer:
    """Defer processing a PR to the next cycle.

    ``pr`` is ``PRState | PartialPRState``: a full derived state when the PR
    enqueued successfully; a partial record when derivation failed (Q4 resolution).
    """

    pr: PRState | PartialPRState
    reason: str


# PEP 604 union — consumers dispatch with ``match`` + ``case _: assert_never(action)``
# (mypy --strict on the decision layer enforces exhaustiveness at lint time).
Action = Activate | Squash | Eject | UpdateComment | Defer


# ---------------------------------------------------------------------------
# Configuration types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AppIdentity:
    """Canonical identity of the merge-queue GitHub App (RFC §4.3.1).

    ``app_id`` is used by ``is_app_identity`` for commit status creator checks.
    ``bot_user_id`` is used by ``is_app_identity_actor`` for timeline event checks
    (timeline events lack the app sub-object; the bot's user_id is stable).

    Both IDs are resolved at App-registration time. For Phase 1 tests, the
    sentinel is ``AppIdentity(slug="rocm-mq", app_id=12345, bot_user_id=99999)``.
    """

    slug: str
    app_id: int
    bot_user_id: int


@dataclass(frozen=True, slots=True)
class MergeQueueConfig:
    """Configuration for the merge queue processor cycle.

    ``all_queues`` is the ordered tuple of all queue names (used by ``decide_cycle``
    for per-queue FIFO bucketing). ``path_to_queues`` is a pre-sorted mapping from
    file path glob to the set of queue names that path belongs to (RFC §4.2).

    String-typed fields have defaults matching the RFC §4.3 label conventions.
    No defaults on structural fields (``all_queues``, ``path_to_queues``, ``app_identity``)
    to avoid accidental aliasing of mutable containers (Pitfall 10 family).
    """

    all_queues: tuple[str, ...]
    path_to_queues: tuple[tuple[str, frozenset[str]], ...]
    app_identity: AppIdentity
    activation_status_context: str = "merge-queue/active"
    queued_label: str = "mq:queued"
    active_label: str = "mq:active"
    label_prefix: str = "mq:"
    # WF-02 at-enqueue ≥1-approval gate. RFC §5 requires this for upstream;
    # the fork-dogfood phase overrides to False via env (MQ_REQUIRE_APPROVAL=0)
    # because the fork has only one collaborator and PR authors cannot self-
    # approve. PORT-02: must be True for upstream port (default already is).
    require_approval_at_enqueue: bool = True


# ---------------------------------------------------------------------------
# Render context — renderer-only fields separated from decision state (D-03)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RenderContext:
    """Renderer-only data for a single PR status comment (D-03).

    These fields are absent from ``PRState`` deliberately — they are not read
    by ``decide_cycle`` and their presence would pollute Hypothesis strategies
    with noise generators for fields that do not affect algorithm output.

    Populated by ``cmd_handle.py`` (Phase 3) / ``cmd_process.py`` (Phase 2).
    """

    author_login: str
    pr_title: str
    queue_positions: tuple[tuple[str, int, int], ...]  # (queue_name, position, depth)
    blockers: tuple[int, ...]  # PR numbers blocking this PR in any queue
    cycle_run_url: str | None
    state: str  # "queued" | "active" | "merged" | "ejected"
    eject_reason: str | None
    merged_sha: str | None


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """Result of executing a single action (Phase 2 executor populates this).

    In Phase 1, this is a stub — renderers tolerate it as optional decoration
    (``success=True``, ``error_message=None`` for hypothetical outcomes in tests).
    """

    action: Action
    success: bool
    error_message: str | None


@dataclass(frozen=True, slots=True)
class CycleRenderContext:
    """Renderer-only data for the per-cycle ``$GITHUB_STEP_SUMMARY`` (D-03).

    Populated by ``cmd_process.py`` (Phase 2) after the cycle completes.
    ``queue_depths`` maps queue name → number of PRs in that queue this cycle.
    """

    cycle_started_at: datetime
    cycle_completed_at: datetime
    cycle_run_url: str | None
    queue_depths: tuple[tuple[str, int], ...]  # (queue_name, depth)
