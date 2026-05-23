"""
rocm_mq.snapshot — I/O adapter that builds a ``RawSnapshot`` from the GitHub
API for the pure decision layer (RFC §4.6).

The pure layer consumes only the frozen ``RawSnapshot`` dataclass; no
githubkit types cross the I/O→pure boundary.

Required-check evaluation is delegated to GitHub branch protection
(RFC §4.8); the executor parses the merge-API 405/422 response to translate
protection-blocked merges into Eject actions. ``incomplete_results=True`` on
any search response aborts the cycle (the stateless processor's next cron
tick retries — RFC §4.6).

Owner/repo are passed as explicit arguments rather than carried on
``MergeQueueConfig`` to avoid widening the dataclass.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import githubkit.exception as _ghkit_exc  # noqa: F401

from rocm_mq._helpers import parse_gh_timestamp
from rocm_mq.state import (
    CommitStatus,
    CommitStatusCreator,
    LabelEvent,
    MergeQueueConfig,
    RawPRState,
    RawSnapshot,
    TimelineActor,
)

if TYPE_CHECKING:
    # GitHubClient is used purely as a type hint; keep the import behind
    # TYPE_CHECKING so the linter does not flag it as unused at runtime.
    from rocm_mq.gh import GitHubClient


# ---------------------------------------------------------------------------
# Cycle-abort exceptions
# ---------------------------------------------------------------------------
class SnapshotIncompleteError(RuntimeError):
    """Search results were incomplete/truncated; abort this processor cycle."""


# Creator bridging
# ---------------------------------------------------------------------------


def _make_status_creator(
    creator: Any | None,  # SimpleUser-like — duck-typed
    config: MergeQueueConfig,
) -> CommitStatusCreator:
    """Bridge a SimpleUser-shaped creator to CommitStatusCreator.

    The commit-status API returns the creator as ``SimpleUser`` (with
    ``login``, ``type``, ``id`` — but no ``app_id`` or ``app_slug``). To make
    ``is_app_identity`` work in the pure decision layer, we infer the App
    identity from the login pattern:

    1. If ``creator is None`` → return an empty stub (``type="User"`` so
       ``is_app_identity`` rejects by type, never by missing field).
    2. If ``creator.type == "Bot"`` AND ``creator.login == f"{slug}[bot]"`` →
       populate ``app_slug`` and ``app_id`` from ``config.app_identity``.
    3. Otherwise → leave ``app_slug`` and ``app_id`` as ``None``. Compares
       creator type + slug to identify App-authored statuses: sibling
       workflow bots (``github-actions[bot]``) and User-type creators must
       never appear to be the merge-queue App.

    Threat: a malicious actor cannot trigger the bridging by spoofing the
    login string — GitHub itself enforces the ``[bot]`` suffix on App bot
    identities, and the audit job's creator-filter cross-checks the per-
    status (App or workflow) classification (RFC §4.3.1).
    """
    if creator is None:
        return CommitStatusCreator(login="", type="User", app_slug=None, app_id=None)
    login = str(getattr(creator, "login", "") or "")
    type_ = str(getattr(creator, "type", "User") or "User")
    expected_bot_login = f"{config.app_identity.slug}[bot]"
    if type_ == "Bot" and login == expected_bot_login:
        return CommitStatusCreator(
            login=login,
            type=type_,
            app_slug=config.app_identity.slug,
            app_id=config.app_identity.app_id,
        )
    return CommitStatusCreator(login=login, type=type_, app_slug=None, app_id=None)


# ---------------------------------------------------------------------------
# RawPRState assembly
# ---------------------------------------------------------------------------


def _make_timeline_actor(actor: Any | None) -> TimelineActor:
    """Build a TimelineActor from a SimpleUser-shaped actor on a timeline event."""
    if actor is None:
        return TimelineActor(login="", type="User", user_id=0)
    return TimelineActor(
        login=str(getattr(actor, "login", "") or ""),
        type=str(getattr(actor, "type", "User") or "User"),
        user_id=int(getattr(actor, "id", 0) or 0),
    )


def _make_raw_pr_state(
    *,
    pr: Any,
    statuses: list[Any],
    timeline: list[Any],
    files: list[Any] | list[str],
    config: MergeQueueConfig,
) -> RawPRState:
    """Assemble a RawPRState from githubkit API objects.

    No I/O. Pure function over already-fetched API responses. Every timestamp
    flows through ``parse_gh_timestamp`` — the single chokepoint that
    guarantees timezone-aware datetimes (avoiding naive-datetime FIFO
    corruption).
    """
    labels = frozenset(label.name for label in (pr.labels or []))

    # head_statuses: bridge SimpleUser creator → CommitStatusCreator.
    head_statuses = tuple(
        CommitStatus(
            context=str(s.context),
            state=str(s.state),
            creator=_make_status_creator(getattr(s, "creator", None), config),
            created_at=parse_gh_timestamp(str(s.created_at)),
        )
        for s in statuses
    )

    # mq_queued_label_events: filter timeline for "labeled" events on the queued label
    queued_label = config.queued_label
    label_events: list[LabelEvent] = []
    for ev in timeline:
        # Defensive: timeline can contain many event shapes; we want only
        # labeled-issue events whose label.name matches the queued label.
        event_kind = getattr(ev, "event", None)
        if event_kind not in ("labeled", "unlabeled"):
            continue
        label_obj = getattr(ev, "label", None)
        if label_obj is None or getattr(label_obj, "name", None) != queued_label:
            continue
        label_events.append(
            LabelEvent(
                label_name=str(label_obj.name),
                event=str(event_kind),
                actor=_make_timeline_actor(getattr(ev, "actor", None)),
                created_at=parse_gh_timestamp(str(ev.created_at)),
            )
        )

    # Required-check results are not fetched here; branch protection enforces
    # them at squash time (RFC §4.8).

    # changed_paths: tuple of file names (may be empty when list_files skipped)
    if files and not isinstance(files[0], str):
        changed = tuple(str(f.filename) for f in files)  # type: ignore[union-attr]
    else:
        changed = tuple(str(f) for f in files)

    return RawPRState(
        number=int(pr.number),
        head_sha=str(pr.head.sha),
        labels=labels,
        head_statuses=head_statuses,
        mq_queued_label_events=tuple(label_events),
        changed_paths=changed,
    )


# ---------------------------------------------------------------------------
# build_snapshot — the public entry point
# ---------------------------------------------------------------------------


def _has_queue_label(labels: frozenset[str], config: MergeQueueConfig) -> bool:
    """Return True if any label starts with the mq:<queue> prefix for a known queue."""
    queue_labels = {f"{config.label_prefix}{q}" for q in config.all_queues}
    return bool(labels & queue_labels)


def build_snapshot(
    client: GitHubClient,
    config: MergeQueueConfig,
    *,
    owner: str,
    repo: str,
) -> RawSnapshot:
    """Build a RawSnapshot from the live GitHub API.

    Workflow (RFC §4.6 + §4.9):
    1. Search each configured queue (``label:mq:<queue> is:pr is:open
       repo:<owner>/<repo>``) and assert ``incomplete_results == False`` on
       every page (guards against silently-truncated PR sets).
    2. Deduplicate PRs by number across queue searches; a PR labelled for
       multiple queues is fetched once.
    3. Per unique PR, fan out: ``pulls.get``, commit statuses, timeline
       events. ``pulls.list_files`` is SKIPPED when the PR already carries any
       ``mq:<queue>`` label — labels are the source of truth once applied
       (RFC §4.2), and refetching paths would burn API budget for no
       decision impact. ``RawPRState.changed_paths`` will be ``()`` in that
       case, and ``derive_pr`` must not depend on path-based queue assignment
       for already-labelled PRs.
    4. Assemble each ``RawPRState`` via ``_make_raw_pr_state`` (pure).

    Args:
        client: GitHubClient — thin wrapper exposing ``.rest``.
        config: MergeQueueConfig — provides queue list, label prefix, and
            ``app_identity`` for the SimpleUser→CommitStatusCreator bridging.
        owner: GitHub repository owner (login).
        repo: GitHub repository name.

    Returns:
        RawSnapshot — frozen tuple of RawPRState, one per unique PR.

    Raises:
        SnapshotIncompleteError: if GitHub search results are incomplete or
            exceed the single-page limit — the cycle MUST abort and let the
            next 3-min cron tick retry.
    """
    # ------------------------------------------------------------------
    # Step 1 + 2 — search and deduplicate PR numbers across queues.
    # ------------------------------------------------------------------
    pr_numbers: list[int] = []  # preserves first-seen order for stable output
    seen: set[int] = set()
    for queue in config.all_queues:
        # GitHub search requires colon-quoting for label values that contain
        # colons (e.g. `mq:queued` → `mq%3Aqueued`). Silent search misses on
        # colon-bearing labels would forget PRs from the queue — an RFC §6
        # head-of-all-queues violation.
        label_value = f"{config.label_prefix}{queue}"
        q = f'is:pr is:open repo:{owner}/{repo} label:"{label_value}"'
        # per_page=100 is the search API maximum; full pagination is a
        # follow-up. The total_count guard below fails loudly if a single
        # queue ever exceeds 100 PRs.
        resp = client.rest.search.issues_and_pull_requests(q=q, per_page=100)
        data = resp.parsed_data
        if getattr(data, "incomplete_results", False):
            raise SnapshotIncompleteError(
                f"search returned incomplete_results=True for queue={queue!r}; "
                "aborting cycle (the next 3-min cron tick retries — RFC §4.6)"
            )
        items = list(getattr(data, "items", []) or [])
        total_count = int(getattr(data, "total_count", len(items)) or len(items))
        if total_count > len(items):
            raise SnapshotIncompleteError(
                f"search for queue={queue!r} returned total_count={total_count} "
                f"but only {len(items)} items fit in per_page=100; pagination "
                "is required. Aborting cycle."
            )
        for item in items:
            number = int(item.number)
            if number in seen:
                continue
            seen.add(number)
            pr_numbers.append(number)

    # ------------------------------------------------------------------
    # Step 3 + 4 — per-PR fan-out and RawPRState assembly.
    # ------------------------------------------------------------------
    raw_prs: list[RawPRState] = []
    for pr_number in pr_numbers:
        pr_resp = client.rest.pulls.get(owner, repo, pr_number)
        pr_obj = pr_resp.parsed_data

        head_sha = str(pr_obj.head.sha)
        labels = frozenset(label.name for label in (pr_obj.labels or []))

        # Commit statuses on the head SHA.
        statuses_resp = client.rest.repos.list_commit_statuses_for_ref(
            owner, repo, head_sha
        )
        statuses_data = statuses_resp.parsed_data
        statuses = list(statuses_data) if statuses_data is not None else []

        # Timeline events for label-based FIFO timestamp source.
        timeline_resp = client.rest.issues.list_events_for_timeline(
            owner, repo, pr_number
        )
        timeline_data = timeline_resp.parsed_data
        timeline = list(timeline_data) if timeline_data is not None else []

        # Skip list_files when the PR is already labelled for a queue —
        # labels are the source of truth once applied (RFC §4.2).
        if _has_queue_label(labels, config):
            files: list[Any] = []
        else:
            files_resp = client.rest.pulls.list_files(owner, repo, pr_number)
            files_data = files_resp.parsed_data
            files = list(files_data) if files_data is not None else []

        raw_prs.append(
            _make_raw_pr_state(
                pr=pr_obj,
                statuses=statuses,
                timeline=timeline,
                files=files,
                config=config,
            )
        )

    return RawSnapshot(prs=tuple(raw_prs))
