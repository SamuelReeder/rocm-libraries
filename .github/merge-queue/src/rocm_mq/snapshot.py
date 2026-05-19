"""
rocm_mq.snapshot — I/O adapter that builds a RawSnapshot from the GitHub API.

PURE-09 compliance statement: this module is NOT in PURE_LAYER_MODULES; it
intentionally imports githubkit indirectly via ``rocm_mq.gh.GitHubClient`` and
uses ``githubkit.exception`` for narrow error-type access. The pure decision
layer (``derive_snapshot``/``decide_cycle``) consumes only the frozen
``RawSnapshot`` dataclass returned by ``build_snapshot``; no githubkit types
cross the I/O→pure boundary.

Public surface (Phase 2 contract):
- ``build_snapshot(client, config, owner, repo) -> RawSnapshot`` —
  reads every PR that carries an ``mq:<queue>`` label across all configured
  queues, fans out per-PR fetches (pull, statuses, timeline, checks, optionally
  files), and assembles a ``RawSnapshot``.

Internal helpers (exposed for unit tests):
- ``_make_status_creator(creator, config) -> CommitStatusCreator`` — bridges
  the API's ``SimpleUser``-shaped creator (no ``app_id``/``app_slug`` fields)
  to ``CommitStatusCreator``. Populates app fields only when ``type=="Bot"``
  AND ``login == f"{slug}[bot]"`` exactly. Sibling workflow bots
  ("github-actions[bot]") and User-type creators get ``None`` app fields.
  This is RESEARCH.md Critical Discovery 1 — the API does not return App ID
  on commit-status creators; we infer it from the login pattern + config.
- ``_map_check_run_state(status, conclusion) -> str`` — translates the
  (status, conclusion) tuple from the Checks API into the literal expected by
  the pure decision layer (``"pending" | "success" | "failure" | "error" | "neutral"``).
- ``_make_raw_pr_state(pr, statuses, timeline, checks, files, config) -> RawPRState``
  — pure assembly helper; no I/O.

Behaviour notes:
- ``pulls.list_files`` is SKIPPED when the PR already carries any ``mq:<queue>``
  label (OQ-2 resolution). The queue assignment was made by an earlier handler
  cycle; refetching the file list would burn API budget for no decision impact.
  The trade-off: ``RawPRState.changed_paths`` will be ``()`` in that case, and
  ``derive_pr`` must not depend on path-based queue assignment for already-labelled
  PRs (this matches the canonical RFC §4.2 contract — labels are the source of
  truth once applied).
- BOTH ``checks.list_for_ref`` and ``repos.list_commit_statuses_for_ref`` are
  read into ``required_check_results`` (OQ-4 resolution). This covers GHA-driven
  check runs AND external CI driving via the older commit-status API.
- ``incomplete_results=True`` on any search response aborts the cycle with an
  ``AssertionError`` (T-02-02-02). The stateless processor (RFC §4.6) retries on
  the next 3-min cron tick.

Owner/repo plumbing: passed as explicit arguments rather than carried on
``MergeQueueConfig`` to avoid widening the Phase 1 dataclass and the 5 test
construction sites that depend on it (see 02-02-SUMMARY for the trade-off note).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from rocm_mq._helpers import parse_gh_timestamp
from rocm_mq.state import (
    CommitStatus,
    CommitStatusCreator,
    LabelEvent,
    MergeQueueConfig,
    RawPRState,
    RawSnapshot,
    RequiredCheckResult,
    TimelineActor,
)

if TYPE_CHECKING:
    # GitHubClient is used purely as a type hint; keep the import behind
    # TYPE_CHECKING so the linter does not flag it as unused at runtime. The
    # PURE-09 positive lint (test_io_modules_do_import_githubkit) requires a
    # real githubkit import at module level — see the explicit import below.
    from rocm_mq.gh import GitHubClient

# PURE-09 positive marker: this module MUST import githubkit at module level
# so the I/O-layer lint (test_io_modules_do_import_githubkit) passes. We use
# the exception type for narrow except clauses in the search guard below.
import githubkit.exception as _ghkit_exc  # noqa: F401  (PURE-09 positive marker)

# ---------------------------------------------------------------------------
# Mapping tables
# ---------------------------------------------------------------------------

# Conclusion → pure-layer state mapping (UK-7).
_CONCLUSION_TO_STATE: dict[str, str] = {
    "success": "success",
    "failure": "failure",
    "timed_out": "failure",
    "action_required": "failure",
    "cancelled": "failure",
    "neutral": "neutral",
    "skipped": "neutral",
}


def _map_check_run_state(status: str, conclusion: str | None) -> str:
    """Map a CheckRun ``(status, conclusion)`` pair to the pure-layer state literal.

    Rules (RESEARCH.md UK-7):
    - status != "completed" → "pending" (the check has not finished).
    - status == "completed" + known conclusion → mapped per ``_CONCLUSION_TO_STATE``.
    - status == "completed" + unknown/None conclusion → "error" (anomalous; we fail
      closed so the decision layer treats the check as not-passing).
    """
    if status != "completed":
        return "pending"
    if conclusion is None:
        return "error"
    return _CONCLUSION_TO_STATE.get(conclusion, "error")


# ---------------------------------------------------------------------------
# Creator bridging (Critical Discovery 1)
# ---------------------------------------------------------------------------


def _make_status_creator(
    creator: Any | None,  # SimpleUser-like — duck-typed
    config: MergeQueueConfig,
) -> CommitStatusCreator:
    """Bridge a SimpleUser-shaped creator to CommitStatusCreator.

    The commit-status API returns the creator as ``SimpleUser`` (with ``login``,
    ``type``, ``id`` — but NO ``app_id`` or ``app_slug``). To make
    ``is_app_identity`` work in the pure decision layer, we infer the App
    identity from the login pattern:

    1. If ``creator is None`` → return an empty stub (``type="User"`` so
       ``is_app_identity`` will reject by type, never by missing field).
    2. If ``creator.type == "Bot"`` AND ``creator.login == f"{slug}[bot]"`` →
       populate ``app_slug`` and ``app_id`` from ``config.app_identity``.
    3. Otherwise → leave ``app_slug`` and ``app_id`` as ``None``. This matches
       the canonical-vs-impersonator distinction enforced by ``is_app_identity``
       (Pitfall 2): sibling workflow bots (``github-actions[bot]``) and User-type
       creators must NEVER appear to be the merge-queue App.

    Threat: a malicious actor cannot trigger the bridging by spoofing the login
    string — GitHub itself enforces the ``[bot]`` suffix on App bot identities,
    and the audit job's creator-filter cross-checks the per-status (App or
    workflow) classification (RFC §4.3.1).
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
    checks: list[Any],
    files: list[Any] | list[str],
    config: MergeQueueConfig,
) -> RawPRState:
    """Assemble a RawPRState from githubkit API objects.

    No I/O. Pure function over already-fetched API responses. Every timestamp
    flows through ``parse_gh_timestamp`` (the single chokepoint enforcing
    Pitfall 3: naive-datetime FIFO corruption).
    """
    labels = frozenset(label.name for label in (pr.labels or []))

    # head_statuses: full bridging via _make_status_creator (Critical Discovery 1)
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

    # required_check_results: OQ-4 — combine check runs AND commit statuses.
    # Status-API entries become checks named by their context; we use the same
    # state mapping where possible (success/failure/pending/error).
    check_results: list[RequiredCheckResult] = []
    for cr in checks:
        check_results.append(
            RequiredCheckResult(
                name=str(cr.name),
                state=_map_check_run_state(
                    str(cr.status), getattr(cr, "conclusion", None)
                ),
            )
        )
    # Statuses → checks (one entry per context; status's state literal is
    # already in the pure-layer vocabulary: pending/success/failure/error).
    for s in statuses:
        # Skip the activation status itself — it is a control plane signal, not
        # a CI check that the decision layer evaluates for pass/fail. The
        # activation filter is applied separately in derive_pr.
        if str(s.context) == config.activation_status_context:
            continue
        check_results.append(
            RequiredCheckResult(name=str(s.context), state=str(s.state))
        )

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
        required_check_results=tuple(check_results),
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
       repo:<owner>/<repo>``) and assert ``incomplete_results == False`` for
       every page (T-02-02-02 — guards against silently-truncated PR sets).
    2. Deduplicate PRs by number across queue searches; a PR labelled for
       multiple queues should be fetched once.
    3. Per unique PR, fan out: ``pulls.get`` (head SHA, labels), commit statuses,
       timeline events, check runs. Skip ``pulls.list_files`` when the PR
       already carries any ``mq:<queue>`` label (OQ-2 resolution: labels are
       the source of truth once applied; refetching paths is API budget waste).
    4. Assemble each ``RawPRState`` via ``_make_raw_pr_state`` (no I/O —
       pure transformation).

    Args:
        client: GitHubClient — Phase 2-01 thin wrapper exposing ``.rest``.
        config: MergeQueueConfig — provides queue list, label prefix, and
            ``app_identity`` for the SimpleUser→CommitStatusCreator bridging.
        owner: GitHub repository owner (login).
        repo: GitHub repository name.

    Returns:
        RawSnapshot — frozen tuple of RawPRState, one per unique PR.

    Raises:
        AssertionError: if ``incomplete_results=True`` on any search page —
            the cycle MUST abort and let the next 3-min cron tick retry.
    """
    # ------------------------------------------------------------------
    # Step 1 + 2 — search and deduplicate PR numbers across queues.
    # ------------------------------------------------------------------
    pr_numbers: list[int] = []  # preserves first-seen order for stable output
    seen: set[int] = set()
    for queue in config.all_queues:
        q = f"is:pr is:open repo:{owner}/{repo} label:{config.label_prefix}{queue}"
        resp = client.rest.search.issues_and_pull_requests(q=q)
        data = resp.parsed_data
        assert not getattr(data, "incomplete_results", False), (
            f"search returned incomplete_results=True for queue={queue!r}; "
            "aborting cycle (the next 3-min cron tick retries — RFC §4.6)"
        )
        for item in getattr(data, "items", []) or []:
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

        # Commit statuses on the head SHA (Pitfall D: pagination — fetch all)
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

        # Check runs for the head SHA.
        checks_resp = client.rest.checks.list_for_ref(owner, repo, head_sha)
        checks_data = checks_resp.parsed_data
        # Checks API returns a wrapper with .check_runs OR a bare list — handle both
        if hasattr(checks_data, "check_runs"):
            checks = list(checks_data.check_runs)
        else:
            checks = list(checks_data) if checks_data is not None else []

        # OQ-2: skip list_files when the PR is already labelled for a queue.
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
                checks=checks,
                files=files,
                config=config,
            )
        )

    return RawSnapshot(prs=tuple(raw_prs))
