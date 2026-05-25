"""
rocm_mq.cmd_handle — Command handler for ``/merge`` and ``/dequeue``
(RFC §4.3-§4.5). Invoked from ``.github/workflows/mq-handler.yml`` on every
``issue_comment: [created]`` event.

Public surface:
  - ``main(argv) -> int`` — CLI entrypoint; reads ``$GITHUB_EVENT_PATH``,
    parses the webhook payload, dispatches to the per-command handler.
  - ``parse_commands(body) -> set[str]`` — per-line ``^/(merge|dequeue)\\s*$``
    parser.
  - ``is_self_bootstrap(changed_paths) -> list[str]`` — intersection of
    changed paths with ``SELF_BOOTSTRAP_PATHS`` (RFC §8).

This module does not import ``rocm_mq.decision`` and does not call
``decide_cycle``. ``/merge`` only labels the PR, posts the status comment,
and posts the eyes reaction; the processor's next cycle discovers the
queued PR via search-by-label and runs the decision algorithm (RFC §4.6).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from githubkit.exception import RequestFailed

from rocm_mq import protected_paths
from rocm_mq.comment import render_status_body
from rocm_mq.executor import _find_status_comment_id
from rocm_mq.pathmap import queues_for_paths
from rocm_mq.state import MergeQueueConfig, PRState, RenderContext

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Per-line exact-match command regex. Humans pasting code snippets that
# quote `/merge` in inline-code do not match (the leading backtick fails
# ``^/`` after .strip()); fenced code blocks containing only ``/merge`` on
# a line do match — intentional permissiveness.
_CMD_RE: re.Pattern[str] = re.compile(r"^/(merge|dequeue)\s*$")

# RFC §4.4: collaborators with these roles may /merge any PR. PR authors are
# always eligible regardless of role (live override in ``_check_perm``).
_ELIGIBLE_ROLES: frozenset[str] = frozenset({"admin", "maintain", "write"})

# At-enqueue known-bad check-run conclusions. Pending and not-yet-reported
# checks are allowed because activation re-runs CI; terminal bad conclusions
# would only burn a full queue cycle before branch protection rejects squash.
_FAILING_CHECK_RUN_CONCLUSIONS: frozenset[str] = frozenset(
    {
        "failure",
        "timed_out",
        "cancelled",
        "action_required",
        "startup_failure",
        "stale",
    }
)

# RFC §4.3 label-name conventions. Mirror ``MergeQueueConfig`` defaults but
# kept as module constants because the idempotency short-circuit runs
# BEFORE the config load.
_LABEL_QUEUED = "mq:queued"
_LABEL_ACTIVE = "mq:active"
_LABEL_PREFIX = "mq:"


# ---------------------------------------------------------------------------
# Public: parse_commands + is_self_bootstrap (independently tested)
# ---------------------------------------------------------------------------


def parse_commands(body: str) -> set[str]:
    """Return the set of recognized commands in a comment body.

    Per-line exact match. Each line is stripped of whitespace and matched
    against ``^/(merge|dequeue)\\s*$``. The set return type means duplicate
    commands collapse to a single entry — the handler responds once per
    distinct command (RFC §4.5 idempotency contract).
    """
    return {
        m.group(1) for line in body.splitlines() if (m := _CMD_RE.match(line.strip()))
    }


def is_self_bootstrap(changed_paths: list[str]) -> list[str]:
    """Return changed paths that intersect direct or generated protected roots.

    Self-bootstrap protection per RFC §8. Comparison is delegated to
    ``rocm_mq.protected_paths`` so spelling variants are normalized for
    comparison while user diagnostics retain GitHub's original changed path
    strings.

    Args:
        changed_paths: List of file paths returned by
            ``pulls.list_files(...).parsed_data[*].filename``.

    Returns:
        Subset of ``changed_paths`` (input order preserved) that matched any
        SELF_BOOTSTRAP glob directly or via an inventoried generated
        source→protected-output pair. Empty list means the PR is safe to
        enqueue from the self-bootstrap perspective.
    """
    direct_hits = protected_paths.find_protected_path_hits(changed_paths)
    generated_hits = protected_paths.find_generated_protected_path_hits(
        changed_paths,
        pairs=protected_paths.GENERATED_PROTECTED_PATH_PAIRS,
    )
    if not direct_hits:
        return generated_hits
    if not generated_hits:
        return direct_hits
    return [
        path for path in changed_paths if path in direct_hits or path in generated_hits
    ]


# ---------------------------------------------------------------------------
# Private: per-step helpers
# ---------------------------------------------------------------------------


def _check_perm(
    client: Any,
    owner: str,
    repo: str,
    username: str,
    *,
    pr_author_login: str,
) -> tuple[bool, str]:
    """Live collaborator permission check + RFC §4.4 PR-author override.

    Returns ``(eligible, role_name)``:
      - ``eligible`` is True when the commenter is the PR author (override),
        or when their live ``repos.get_collaborator_permission_level``
        role is in ``_ELIGIBLE_ROLES``.
      - ``role_name`` is the live role string from the API
        (``admin|maintain|write|triage|read|none``), or ``"none"`` when the
        API returns 404 (not a collaborator).

    Uses the live collaborator permission API, not ``author_association``,
    because ``author_association`` is computed at comment-write time and
    does not reflect collaborators added after the fork was created (or
    any subsequent org / collaborator state change).
    """
    # Probe the live role first. On 404 (not a collaborator) we still need
    # the PR-author override to make the decision.
    role_name: str
    try:
        resp = client.rest.repos.get_collaborator_permission_level(
            owner, repo, username
        )
        role_name = str(resp.parsed_data.role_name)
    except RequestFailed as exc:
        # Treat 404 as "not a collaborator → role: none". Any other status
        # code propagates (the main() try/except catches and returns 1).
        if getattr(getattr(exc, "response", None), "status_code", None) != 404:
            raise
        role_name = "none"

    # Eligibility: PR-author override wins regardless of role; otherwise
    # the role must be in the eligible set.
    if username == pr_author_login or role_name in _ELIGIBLE_ROLES:
        return True, role_name
    return False, role_name


def _check_at_enqueue_gates(
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    pr: Any,
    queues: frozenset[str],
    *,
    require_approval: bool = True,
) -> list[str]:
    """Check the at-enqueue gates (RFC §4.3). Returns a list of failed gate
    names; empty means all passed.

    All gates are checked even if earlier ones fail so the rejection comment
    shows the user every blocker at once.

    Gates:
      1. ``empty-queue-set`` — derived queue set is empty. (Also handled
         separately in main() with a more specific message; re-checked
         here for safety.)
      2. ``maintainer-edits-disabled`` — ``pr.maintainer_can_modify`` is
         False on a cross-repo PR (the queue's develop-merge push needs
         the upstream maintainer to have push access to the fork branch).
         Only checked on cross-repo PRs — see ``maintainer_can_modify``
         note below.
      3. ``no-approval`` — ``pulls.list_reviews`` carries zero entries
         whose ``state == "APPROVED"`` (RFC §5: required review state).
      4. ``failing-required-check`` — legacy commit statuses report any
         context in {"failure", "error"}, or check-runs report a terminal
         bad conclusion.
    """
    failed: list[str] = []

    if not queues:
        failed.append("empty-queue-set")

    # The `maintainer_can_modify` field is only meaningful for cross-repo
    # PRs. For same-repo PRs GitHub returns `false` by default — the field
    # has no meaning because the PR head IS in the maintainer's repo, and
    # gating on it would reject legitimate same-repo PRs. Restrict the
    # check to cross-repo PRs where the field carries real signal (the
    # fork author must opt in to let the upstream maintainer push to their
    # branch for the queue's develop-merge step).
    head_repo_id = getattr(getattr(getattr(pr, "head", None), "repo", None), "id", None)
    base_repo_id = getattr(getattr(getattr(pr, "base", None), "repo", None), "id", None)
    is_cross_repo = (
        head_repo_id is not None
        and base_repo_id is not None
        and head_repo_id != base_repo_id
    )
    if is_cross_repo and not getattr(pr, "maintainer_can_modify", False):
        failed.append("maintainer-edits-disabled")

    # No-approval gate (RFC §5). Behavior is config-toggled via
    # ``MergeQueueConfig.require_approval_at_enqueue`` (default True
    # preserves the upstream contract; PORT-02 closure — the config flag
    # replaces a code-level comment-out so the gate cannot silently
    # regress at upstream port time).
    if require_approval:
        reviews_resp = client.rest.pulls.list_reviews(owner, repo, pr_number)
        reviews = list(reviews_resp.parsed_data or [])
        has_approval = any(getattr(r, "state", "") == "APPROVED" for r in reviews)
        if not has_approval:
            failed.append("no-approval")

    head_sha = getattr(getattr(pr, "head", None), "sha", "")
    if head_sha:
        status_resp = client.rest.repos.get_combined_status_for_ref(
            owner, repo, head_sha
        )
        per_context = list(getattr(status_resp.parsed_data, "statuses", []) or [])
        failing_required_check = any(
            getattr(s, "state", "") in {"failure", "error"} for s in per_context
        )

        checks_resp = client.rest.checks.list_for_ref(owner, repo, head_sha)
        check_runs = list(getattr(checks_resp.parsed_data, "check_runs", []) or [])
        failing_required_check = failing_required_check or any(
            getattr(run, "conclusion", None) in _FAILING_CHECK_RUN_CONCLUSIONS
            for run in check_runs
        )
        if failing_required_check:
            failed.append("failing-required-check")

    return failed


def _apply_labels(
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    queues: frozenset[str],
) -> None:
    """Apply ``mq:queued`` + ``mq:<queue>`` for each derived queue. Idempotent."""
    labels = [_LABEL_QUEUED, *sorted(f"{_LABEL_PREFIX}{q}" for q in queues)]
    client.rest.issues.add_labels(owner, repo, pr_number, labels=labels)


def _remove_mq_labels(
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    pr: Any,
) -> None:
    """Remove every ``mq:*`` label currently on the PR (dequeue path).

    ``remove_label`` is per-label and raises 404 if the label is absent —
    we read the current label set off the freshly-fetched PR and only
    issue the removes for labels actually present.
    """
    current = {str(getattr(lbl, "name", "")) for lbl in getattr(pr, "labels", [])}
    for name in sorted(n for n in current if n.startswith(_LABEL_PREFIX)):
        try:
            client.rest.issues.remove_label(owner, repo, pr_number, name)
        except RequestFailed as exc:
            # 404 = label already gone (race vs another handler invocation
            # or the audit job). Safe to ignore.
            if getattr(getattr(exc, "response", None), "status_code", None) == 404:
                continue
            raise


def _upsert_status_comment(
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    pr: Any,
    queues: frozenset[str],
    *,
    state_name: str,
    author_login: str,
    now: datetime,
) -> None:
    """Build a RenderContext for the given state and upsert the status comment."""
    head_sha = str(getattr(getattr(pr, "head", None), "sha", ""))
    # Build a minimal PRState — render_queued only reads enqueued_at and
    # ignores most other fields. Use ``now`` as the enqueued timestamp
    # (the processor's next cycle will derive the canonical timestamp
    # from the label timeline event).
    pr_state = PRState(
        number=pr_number,
        head_sha=head_sha,
        labels=frozenset(),
        queues=queues,
        enqueued_at=now,
        is_validly_active=False,
    )
    # queue_positions: the handler does not know cycle-time queue depths,
    # so we render placeholder rows showing "pending" position. The
    # processor will overwrite the comment with accurate positions on the
    # next cycle.
    queue_positions: tuple[tuple[str, int, int], ...] = tuple(
        (q, 0, 0) for q in sorted(queues)
    )
    ctx = RenderContext(
        author_login=author_login,
        pr_title="",  # handler does not read PR title; placeholder
        queue_positions=queue_positions,
        blockers=(),
        cycle_run_url=None,
        state=state_name,
        eject_reason=None,
        merged_sha=None,
    )
    body = render_status_body(pr_state, ctx, now)

    comment_id = _find_status_comment_id(client, owner, repo, pr_number)
    if comment_id is None:
        client.rest.issues.create_comment(owner, repo, pr_number, body=body)
    else:
        client.rest.issues.update_comment(owner, repo, comment_id, body=body)


def _post_eyes_reaction(
    client: Any,
    owner: str,
    repo: str,
    comment_id: int,
) -> None:
    """Post the eyes reaction on the trigger comment.

    The reactions API returns 200 on duplicate, 201 on first — both are
    success, no pre-check needed.
    """
    client.rest.reactions.create_for_issue_comment(
        owner, repo, comment_id, content="eyes"
    )


def _post_comment(
    client: Any, owner: str, repo: str, pr_number: int, body: str
) -> None:
    """Wrapper around ``issues.create_comment`` for rejection-path comments."""
    client.rest.issues.create_comment(owner, repo, pr_number, body=body)


# ---------------------------------------------------------------------------
# Client + config builders (monkeypatched in tests)
# ---------------------------------------------------------------------------


def _build_client(token: str) -> Any:
    """Construct a real ``GitHubClient`` from the supplied installation token.

    Lazy-imported so test code paths that monkeypatch this helper never
    pay the ``githubkit`` import cost.
    """
    from rocm_mq.gh import GitHubClient

    return GitHubClient(token=token)


def _build_default_config(owner: str, repo: str) -> MergeQueueConfig:
    """Fallback MergeQueueConfig used when ``_load_config`` is not reached.

    Currently unused at runtime (the handler always calls ``_load_config``
    in main); retained for symmetry with ``cmd_process._build_default_config``
    so a future refactor can swap layers without changing the handler API.
    """
    from rocm_mq.gh import resolve_app_identity

    # Note: this path requires a real client; the caller is responsible.
    _ = owner, repo, resolve_app_identity
    raise NotImplementedError(
        "cmd_handle does not call this path; tests monkeypatch _load_config."
    )


def _load_config(client: Any, owner: str, repo: str) -> MergeQueueConfig:
    """Load PATH_TO_QUEUES from develop + resolve the App identity.

    Thin delegate to ``rocm_mq.config.build_config_from_develop`` so the
    handler and processor share one source of truth for the YAML→config
    translation. Tests monkeypatch this helper to inject a canned config
    and avoid the Contents API round-trip.
    """
    from rocm_mq.config import build_config_from_develop

    return build_config_from_develop(client, owner, repo)


# ---------------------------------------------------------------------------
# Per-event handlers (called from main())
# ---------------------------------------------------------------------------


def _handle_merge(
    client: Any,
    config: MergeQueueConfig,
    *,
    owner: str,
    repo: str,
    pr_number: int,
    commenter_login: str,
    comment_id: int,
    now: datetime,
) -> int:
    """End-to-end /merge dispatch. Returns 0 on success, non-zero on error.

    Step order is load-bearing:
      0. Eyes-reaction — posted first, before any branching, so every
         downstream rejection branch implicitly carries the ack.
      1. Read PR (need labels for idempotency + author for perm override).
      2. Idempotency short-circuit (``mq:queued`` / ``mq:active`` present →
         comment upsert only; no labels, no perm check, no gates).
      3. Self-bootstrap rejection (RFC §8) — before any state mutation.
      4. Permission check (live API; PR-author override).
      5. Queue derivation (``pulls.list_files`` → ``queues_for_paths``).
      6. At-enqueue gates (≥1 approval, no failing required check,
         maintainer-edits, queue set non-empty).
      7. Label apply + status-comment upsert.
    """
    # Step 0: post the reaction before any state mutation — idempotent on
    # re-delivery (reactions API returns 200 on duplicate, 201 on first).
    _post_eyes_reaction(client, owner, repo, comment_id)

    pr_resp = client.rest.pulls.get(owner, repo, pr_number)
    pr = pr_resp.parsed_data
    pr_author_login = str(getattr(getattr(pr, "user", None), "login", ""))
    current_labels = {
        str(getattr(lbl, "name", "")) for lbl in getattr(pr, "labels", [])
    }

    # Step 2: idempotency short-circuit. Eyes already posted above.
    if _LABEL_QUEUED in current_labels or _LABEL_ACTIVE in current_labels:
        # Re-derive queues for the comment upsert; this is cheap and lets the
        # second /merge refresh the status comment if anything has drifted.
        files_resp = client.rest.pulls.list_files(owner, repo, pr_number)
        changed_paths = tuple(str(f.filename) for f in (files_resp.parsed_data or []))
        queues = queues_for_paths(changed_paths, config)
        _upsert_status_comment(
            client,
            owner,
            repo,
            pr_number,
            pr,
            queues,
            state_name="queued",
            author_login=pr_author_login,
            now=now,
        )
        return 0

    # Step 3: self-bootstrap rejection. No queue-state mutation on rejection.
    files_resp = client.rest.pulls.list_files(owner, repo, pr_number)
    changed_paths_list: list[str] = [
        str(f.filename) for f in (files_resp.parsed_data or [])
    ]
    bootstrap_hits = is_self_bootstrap(changed_paths_list)
    if bootstrap_hits:
        body = (
            "/merge cannot enqueue this PR because it modifies merge-queue "
            f"infrastructure paths: {', '.join(f'`{p}`' for p in bootstrap_hits)}. "
            "Changes to these paths require manual maintainer review. Once reviewed, "
            "a maintainer can merge directly via the GitHub UI."
        )
        _post_comment(client, owner, repo, pr_number, body)
        return 0

    # Step 4: permission check (live API; never author_association).
    eligible, role_name = _check_perm(
        client,
        owner,
        repo,
        commenter_login,
        pr_author_login=pr_author_login,
    )
    if not eligible:
        body = (
            f"`/merge` rejected: @{commenter_login} has role `{role_name}` on "
            f"this repository, which is not in the eligible set "
            f"({', '.join(sorted(_ELIGIBLE_ROLES))}) and you are not the PR "
            "author. The merge queue requires write-or-above permission to "
            "enqueue another author's PR."
        )
        _post_comment(client, owner, repo, pr_number, body)
        return 0

    # Step 5: derive the per-PR queue set.
    changed_paths = tuple(changed_paths_list)
    queues = queues_for_paths(changed_paths, config)

    # No opted-in path — single, more specific comment than the generic
    # gate failure message.
    if not queues:
        body = (
            "`/merge` rejected: this PR touches no opted-in queue paths "
            "(per `.github/merge-queue/path_to_queues.yml`). Only PRs whose "
            "changes land under an opted-in path tree can be enqueued. "
            "Maintainers may merge non-opted-in changes via the GitHub UI."
        )
        _post_comment(client, owner, repo, pr_number, body)
        return 0

    # Step 6: at-enqueue gates.
    failed_gates = _check_at_enqueue_gates(
        client,
        owner,
        repo,
        pr_number,
        pr,
        queues,
        require_approval=config.require_approval_at_enqueue,
    )
    if failed_gates:
        body = (
            "`/merge` rejected: the following at-enqueue gates failed: "
            f"{', '.join(f'`{g}`' for g in failed_gates)}. Re-post `/merge` "
            "once each condition is addressed."
        )
        _post_comment(client, owner, repo, pr_number, body)
        return 0

    # Step 7: success — label apply, status comment. Eyes already posted at
    # Step 0 so we don't double-post here.
    _apply_labels(client, owner, repo, pr_number, queues)
    _upsert_status_comment(
        client,
        owner,
        repo,
        pr_number,
        pr,
        queues,
        state_name="queued",
        author_login=pr_author_login,
        now=now,
    )
    return 0


def _handle_dequeue(
    client: Any,
    config: MergeQueueConfig,
    *,
    owner: str,
    repo: str,
    pr_number: int,
    commenter_login: str,
    comment_id: int,
    now: datetime,
) -> int:
    """End-to-end /dequeue dispatch. Returns 0 on success.

    RFC §4.4 uses the same permission boundary as ``/merge``: the PR author
    or any collaborator with write/maintain/admin may dequeue. Unauthorized
    requests are rejected before any visible ack reaction, label removal, or
    status-comment update.
    """
    pr_resp = client.rest.pulls.get(owner, repo, pr_number)
    pr = pr_resp.parsed_data
    pr_author_login = str(getattr(getattr(pr, "user", None), "login", ""))

    eligible, role_name = _check_perm(
        client,
        owner,
        repo,
        commenter_login,
        pr_author_login=pr_author_login,
    )
    if not eligible:
        body = (
            f"`/dequeue` rejected: @{commenter_login} has role `{role_name}` on "
            f"this repository, which is not in the eligible set "
            f"({', '.join(sorted(_ELIGIBLE_ROLES))}) and you are not the PR "
            "author. The merge queue requires write-or-above permission to "
            "dequeue another author's PR."
        )
        _post_comment(client, owner, repo, pr_number, body)
        return 0

    _remove_mq_labels(client, owner, repo, pr_number, pr)

    # Re-derive queues for the comment upsert (queue set is informational
    # only on dequeue; cleared labels are the load-bearing signal).
    files_resp = client.rest.pulls.list_files(owner, repo, pr_number)
    changed_paths = tuple(str(f.filename) for f in (files_resp.parsed_data or []))
    queues = queues_for_paths(changed_paths, config)

    head_sha = str(getattr(getattr(pr, "head", None), "sha", ""))
    pr_state = PRState(
        number=pr_number,
        head_sha=head_sha,
        labels=frozenset(),
        queues=queues,
        enqueued_at=now,
        is_validly_active=False,
    )
    ctx = RenderContext(
        author_login=pr_author_login,
        pr_title="",
        queue_positions=(),
        blockers=(),
        cycle_run_url=None,
        state="ejected",
        eject_reason="/dequeue requested by commenter",
        merged_sha=None,
    )
    body = render_status_body(pr_state, ctx, now)
    existing = _find_status_comment_id(client, owner, repo, pr_number)
    if existing is None:
        client.rest.issues.create_comment(owner, repo, pr_number, body=body)
    else:
        client.rest.issues.update_comment(owner, repo, existing, body=body)

    _post_eyes_reaction(client, owner, repo, comment_id)
    return 0


# ---------------------------------------------------------------------------
# main() — argparse + event payload reader + dispatch
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="rocm-mq-handle",
        description=(
            "Command handler — dispatches /merge and /dequeue from a "
            "GitHub issue_comment webhook payload."
        ),
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help="GitHub repository in OWNER/REPO form.",
    )
    parser.add_argument(
        "--event-path",
        default=os.environ.get("GITHUB_EVENT_PATH", ""),
        help="Path to the GHA event JSON payload (issue_comment).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint — see module docstring for the full flow.

    Returns:
      - 0 on every "handled" outcome (successful enqueue, idempotent
        short-circuit, self-bootstrap rejection, perm rejection, gate
        rejection, no-opted-in-path rejection, non-/merge comment skip,
        non-created event skip, non-PR-comment skip).
      - 1 on any uncaught exception (traceback printed to stderr).
      - 2 on usage errors (malformed --repo or missing GITHUB_TOKEN).
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

    # Validate GITHUB_TOKEN (the workflow sets this to the App-minted
    # installation token via ``steps.app-token.outputs.token``).
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print(
            "error: GITHUB_TOKEN environment variable is not set; the "
            "workflow must mint an App installation token via "
            "actions/create-github-app-token and export it as GITHUB_TOKEN.",
            file=sys.stderr,
        )
        return 2

    try:
        return _run(args, owner, repo, token)
    except Exception as exc:
        # Preserve the full traceback so operators debugging a production
        # failure get module:line attribution, not just repr(exc).
        print(f"error: {type(exc).__name__}: {exc!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1


def _run(args: argparse.Namespace, owner: str, repo: str, token: str) -> int:
    """Inner runner — separates the try/except wrapper from the work.

    Reads the event payload, performs the cheap event-shape skips
    (non-created action, non-PR issue comment, no recognized command),
    then dispatches to the per-command handler.
    """
    event_path = args.event_path
    with open(event_path, encoding="utf-8") as fh:
        event = json.load(fh)

    # Skip 1: only ``created`` issue_comment events trigger the handler.
    if event.get("action") != "created":
        return 0

    # Skip 2: only PR comments (issue payloads carrying ``pull_request``)
    # trigger the handler — plain issue comments are out of scope.
    issue = event.get("issue") or {}
    if not isinstance(issue.get("pull_request"), dict):
        return 0

    comment = event.get("comment") or {}
    body = str(comment.get("body") or "")
    commands = parse_commands(body)
    # Skip 3: comment carries no recognized command.
    if not commands:
        return 0

    pr_number = int(issue.get("number") or 0)
    comment_id = int(comment.get("id") or 0)
    commenter_login = str((comment.get("user") or {}).get("login") or "")

    # Build the client + load config (test fixtures monkeypatch these).
    client = _build_client(token)
    config = _load_config(client, owner, repo)
    now = datetime.now(tz=UTC)

    # Dispatch. /merge wins precedence if both are present (the second
    # command is then a no-op idempotent path).
    if "merge" in commands:
        return _handle_merge(
            client,
            config,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            commenter_login=commenter_login,
            comment_id=comment_id,
            now=now,
        )
    if "dequeue" in commands:
        return _handle_dequeue(
            client,
            config,
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            commenter_login=commenter_login,
            comment_id=comment_id,
            now=now,
        )
    return 0


__all__ = [
    "is_self_bootstrap",
    "main",
    "parse_commands",
]
