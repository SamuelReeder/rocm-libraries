"""
rocm_mq.state — Frozen-dataclass state model for the federated merge queue.

All dataclasses in this module are ``@dataclass(frozen=True, slots=True)``.
All collections are ``tuple[T, ...]`` or ``frozenset[T]`` — never ``list``, ``set``,
or ``dict`` (avoids mutable-default aliasing; RFC §4.9 pure-layer contract).

Two PR dataclass families:
  Raw*   — mirror the GitHub API response shape; constructed by the snapshot loader.
  Derived — ``PRState``, ``Snapshot``; output of ``derive_pr``/``derive_snapshot``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

# ---------------------------------------------------------------------------
# Raw family — mirror the GitHub API, unfiltered
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
    filter and the creator (App-identity) filter — see RFC §4.3.1 (the
    activation status binding is only trusted when posted by the App identity).
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
    are the canonical FIFO timestamp source (RFC §4.6 enqueued_at).
    """

    label_name: str
    event: str  # "labeled" | "unlabeled"
    actor: TimelineActor
    created_at: datetime


@dataclass(frozen=True, slots=True)
class RawPRState:
    """Raw GitHub API state for a single PR — unfiltered, unprocessed.

    ``head_statuses`` carries EVERY commit status on the current head SHA.
    ``mq_queued_label_events`` carries EVERY ``mq:queued`` label timeline event.
    The pure ``derive_pr`` applies all filtering (context + creator).

    Required CI check evaluation is NOT done by the queue — branch protection
    is the single source of truth for which checks gate a merge (RFC §4.8);
    the executor translates a protection-blocked merge-API response into an
    Eject action.
    """

    number: int
    head_sha: str
    labels: frozenset[str]
    head_statuses: tuple[CommitStatus, ...]
    mq_queued_label_events: tuple[LabelEvent, ...]
    changed_paths: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RawSnapshot:
    """A raw snapshot of all relevant PRs fetched from the GitHub API."""

    prs: tuple[RawPRState, ...]


# ---------------------------------------------------------------------------
# Derived family — output of ``derive_pr`` / ``derive_snapshot``
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PRState:
    """Derived PR state — contains only fields the decision algorithm reads.

    Renderer-only fields (``author_login``, PR title, blocker PR numbers, run URL)
    are explicitly absent — they live in ``RenderContext`` so Hypothesis
    strategies for ``PRState`` stay free of noise that does not affect
    decision-layer output.

    ``enqueued_at`` must be tz-aware; ``is_validly_active`` is computed by
    ``derive_pr`` applying BOTH context and creator filters (RFC §4.3.1).
    """

    number: int
    head_sha: str
    labels: frozenset[str]
    queues: frozenset[str]  # queue names this PR belongs to (from pathmap + labels)
    enqueued_at: datetime  # must be tz-aware; FIFO sort key
    is_validly_active: bool


@dataclass(frozen=True, slots=True)
class Snapshot:
    """Post-derive flat tuple of derived PR states — input to ``decide_cycle``.

    ``now`` is NOT embedded here; it is passed as a separate arg to ``decide_cycle``
    so Hypothesis tests can vary time independently of PR fixtures.
    """

    prs: tuple[PRState, ...]


# ---------------------------------------------------------------------------
# Derive failure / partial sum types
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DeferredPR:
    """Second arm of ``derive_pr``'s return type.

    Returned when raw derivation cannot produce a full ``PRState`` — e.g., when
    no qualifying ``mq:queued`` label-application event exists yet because of
    GitHub timeline eventual consistency.
    """

    number: int
    reason: str


@dataclass(frozen=True, slots=True)
class PartialPRState:
    """Minimal PR record used as the second arm of ``Defer.pr``.

    Carried when the executor emitted a ``Defer`` action for a PR that could not
    be fully derived. Exported so downstream callers can type-check
    ``Defer.pr`` as ``PRState | PartialPRState``.
    """

    number: int
    head_sha: str
    labels: frozenset[str]


# ---------------------------------------------------------------------------
# Action union — PEP 604, variant dataclasses + assert_never exhaustiveness
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
    enqueued successfully; a partial record when derivation failed.
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

    Both IDs are resolved at App-registration time. Unit tests use the
    sentinel ``AppIdentity(slug="rocm-mq", app_id=12345, bot_user_id=99999)``.
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
    to avoid accidental aliasing of mutable containers.
    """

    all_queues: tuple[str, ...]
    path_to_queues: tuple[tuple[str, frozenset[str]], ...]
    app_identity: AppIdentity
    activation_status_context: str = "merge-queue/active"
    queued_label: str = "mq:queued"
    active_label: str = "mq:active"
    label_prefix: str = "mq:"
    # At-enqueue ≥1-approval gate (RFC §5). DOGFOOD-ONLY: the fork overrides
    # this to False via MQ_REQUIRE_APPROVAL=0 because the fork has only one
    # collaborator and PR authors cannot self-approve. PORT-02: must be True
    # for the upstream port (default already is).
    require_approval_at_enqueue: bool = True


# ---------------------------------------------------------------------------
# Render context — renderer-only fields separated from decision state
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RenderContext:
    """Renderer-only data for a single PR status comment.

    These fields are absent from ``PRState`` deliberately — they are not read
    by ``decide_cycle`` and their presence would pollute Hypothesis strategies
    with noise generators for fields that do not affect algorithm output.

    Populated at the I/O boundary (handler and processor entry points).
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
    """Result of executing a single action — populated by the executor."""

    action: Action
    success: bool
    error_message: str | None


@dataclass(frozen=True, slots=True)
class CycleRenderContext:
    """Renderer-only data for the per-cycle ``$GITHUB_STEP_SUMMARY``.

    Populated by the processor entry point after the cycle completes.
    ``queue_depths`` maps queue name → number of PRs in that queue this cycle.
    """

    cycle_started_at: datetime
    cycle_completed_at: datetime
    cycle_run_url: str | None
    queue_depths: tuple[tuple[str, int], ...]  # (queue_name, depth)
