"""
rocm_mq.executor — Action dispatcher; executes GitHub mutations for each
``Action`` produced by ``decide_cycle``. This module imports ``githubkit``;
pure-layer modules must not.

Public surface:
- ``dispatch(action, client, config, owner, repo) -> ActionOutcome`` —
  exhaustive ``match`` over the five Action variants (``case _:
  assert_never(action)`` ensures a future variant fails at lint time).

Private helpers (exposed for unit tests):
- ``_handle_activate`` — RFC §4.9 activation state machine: merge develop into
  the PR head, re-read to catch author races, flip labels, then stamp the
  App-created activation status last.
- ``_handle_squash`` — squash-merge then verify the resulting commit.
- ``_handle_squash_failure`` — translate merge-API 4xx codes into no-op,
  retry-next-cycle, or inline eject.
- ``_verify_squash`` — parent-linkage check + tree-diff sanity check; raises
  ``CorruptSquashError`` on either failure.
- ``_handle_eject`` — overwrite activation status to ``failure`` and clear all
  ``mq:*`` labels; idempotent.
- ``_handle_update_comment`` / ``_find_status_comment_id`` — upsert the
  ``<!-- rocm-mq-status -->`` comment by marker discovery.
- ``_safe_remove_label`` — wraps ``issues.remove_label`` and swallows 404
  (already-absent is the desired post-state).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar, assert_never

# This module imports githubkit; pure-layer modules must not.
from githubkit.exception import RequestFailed

from datetime import UTC, datetime

from rocm_mq.comment import render_status_body
from rocm_mq.gh import CorruptSquashError
from rocm_mq.state import (
    Action,
    ActionOutcome,
    Activate,
    Defer,
    Eject,
    MergeQueueConfig,
    PRState,
    RenderContext,
    Squash,
    UpdateComment,
)

if TYPE_CHECKING:
    from rocm_mq.gh import GitHubClient


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Must stay byte-identical to ``rocm_mq.comment._STATUS_MARKER`` — the renderer
# embeds it in every comment body and ``_find_status_comment_id`` greps for the
# exact substring; any drift breaks the upsert silently.
_STATUS_MARKER = "<!-- rocm-mq-status -->"

# Trunk branch per RFC §4.9 ("develop" by contract).
_TRUNK_BRANCH = "develop"

# Retries for post-squash readbacks (read-replication lag).
_VERIFY_SQUASH_RETRIES = 3
_VERIFY_SQUASH_BACKOFFS: tuple[float, ...] = (1.0, 2.0)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def dispatch(
    action: Action,
    *,
    client: GitHubClient,
    config: MergeQueueConfig,
    owner: str,
    repo: str,
) -> ActionOutcome:
    """Dispatch a single ``Action`` to its handler.

    Exhaustive ``match`` over the five Action variants. ``Defer`` is a no-op
    (the decision layer signalled "skip this PR for the current cycle"; the
    next 3-min cron tick will reconsider it, per RFC §4.6).

    Returns ``ActionOutcome`` with ``success`` set per the handler's verdict.
    """
    match action:
        case Activate(pr=pr):
            return _handle_activate(pr, client, config, owner, repo)
        case Squash(pr=pr):
            return _handle_squash(pr, client, config, owner, repo)
        case Eject(pr=pr, reason=reason):
            return _handle_eject(pr, reason, client, config, owner, repo)
        case UpdateComment(pr=pr, new_body=body):
            return _handle_update_comment(pr, body, client, config, owner, repo)
        case Defer():
            # No-op by design — the next cycle will reconsider this PR.
            return ActionOutcome(action=action, success=True, error_message=None)
        case _:
            assert_never(action)


# ---------------------------------------------------------------------------
# Activation handler — RFC §4.9 state machine
# ---------------------------------------------------------------------------


def _handle_activate(
    pr: PRState,
    client: GitHubClient,
    config: MergeQueueConfig,
    owner: str,
    repo: str,
) -> ActionOutcome:
    """Run the activation state machine: merge develop → stamp → flip labels.

    The pre-stamp race check defends against an author push landing between the
    develop merge and the activation stamp: if the head SHA we're about to
    stamp is no longer the PR's current head, abort rather than mark a stale
    SHA as active.
    """
    action = Activate(pr=pr)

    # Step 1: pull develop into the PR branch. The base is the PR's head ref
    # (the branch we want to merge develop INTO); the head is "develop". We
    # read the PR's head ref via pulls.get because PRState doesn't carry the
    # branch name.
    pr_obj = _read_pr(client, owner, repo, pr.number)
    pr_branch = str(pr_obj.head.ref)

    try:
        merge_resp = client.rest.repos.merge(
            owner,
            repo,
            base=pr_branch,
            head=_TRUNK_BRANCH,
        )
    except RequestFailed as exc:
        # 409 = merge conflict at activation; documented eject path (RFC §6).
        if _status_code(exc) == 409:
            reason = "merge conflict with develop"
            _handle_eject(pr, reason, client, config, owner, repo)
            return ActionOutcome(
                action=Eject(pr=pr, reason=reason),
                success=False,
                error_message=f"activation failed → ejected: {reason}",
            )
        raise

    # Determine the SHA we will stamp.
    if getattr(merge_resp, "status_code", None) == 204 or merge_resp.parsed_data is None:
        # 204: develop already contained in the PR branch; nothing was created.
        # Fall back to the PR's current head SHA.
        pr_obj_post = _read_pr(client, owner, repo, pr.number)
        new_sha = str(pr_obj_post.head.sha)
    else:
        new_sha = str(merge_resp.parsed_data.sha)

    # Step 2: re-read the PR head after merging develop in to catch author
    # pushes that landed between the merge and the stamp. The check runs
    # unconditionally on both 201 and 204 paths — the race window between
    # determining `new_sha` and calling create_commit_status is two separate
    # API round-trips either way. A mismatch means the author pushed on top
    # of the merge commit (201) or in the small window between the two
    # pulls.get calls (204); either way, never stamp a stale SHA as active.
    pr_obj_check = _read_pr(client, owner, repo, pr.number)
    current_head = str(pr_obj_check.head.sha)
    if current_head != new_sha:
        return ActionOutcome(
            action=action,
            success=False,
            error_message=(
                f"pre-stamp race: PR #{pr.number} head advanced from "
                f"{new_sha!r} to {current_head!r} between develop merge and stamp"
            ),
        )

    # Step 3: label flip before stamp. A failed stamp leaves the PR without
    # an activation status, so the next cycle re-activates from a clean state
    # — add_labels is idempotent (set semantics) and _safe_remove_label
    # swallows 404. The reverse order (stamp → flip) would split-brain the PR
    # on any label-call failure (status=active while labels=queued), and the
    # decision layer would then schedule a Squash for a PR whose human-visible
    # state was still in-queue.
    client.rest.issues.add_labels(
        owner,
        repo,
        pr.number,
        data=[config.active_label],
    )
    _safe_remove_label(client, owner, repo, pr.number, config.queued_label)

    # Step 4: stamp the activation status last — this is the commit point.
    # Posting the App's own success status is what binds activation per
    # RFC §4.9. If this call raises, the labels are already flipped but no
    # activation evidence exists; the next cycle's derive_pr will not
    # classify the PR as validly active and will re-Activate (the merge is
    # a 204 no-op, the label adds are idempotent, and this stamp is retried).
    client.rest.repos.create_commit_status(
        owner,
        repo,
        new_sha,
        state="success",
        context=config.activation_status_context,
    )

    return ActionOutcome(action=action, success=True, error_message=None)


# ---------------------------------------------------------------------------
# Squash handler
# ---------------------------------------------------------------------------


def _handle_squash(
    pr: PRState,
    client: GitHubClient,
    config: MergeQueueConfig,
    owner: str,
    repo: str,
) -> ActionOutcome:
    """Squash-merge ``pr`` and verify the post-squash parent SHA.

    On merge-API failure, translate GitHub's response into either:
      * **no-op** (idempotent re-merge of an already-merged PR; pending
        required check — wait for next cycle), or
      * **inline eject** (failing required check, missing approval, merge
        conflict, head-SHA-changed-during-attempt).

    Branch protection is the source of truth for required checks (RFC §4.8);
    the queue tries the merge and reads GitHub's error response to decide
    rather than pre-evaluating checks itself.
    """
    action = Squash(pr=pr)

    # Record the develop tip before squash; assert it matches the parent
    # commit after — see ``_verify_squash``.
    pre_squash_develop_sha = _read_branch_tip(client, owner, repo, _TRUNK_BRANCH)

    # Squash-merge. GitHub returns various 4xx codes when the merge is not
    # currently permitted; _handle_squash_failure translates them.
    try:
        merge_resp = client.rest.pulls.merge(
            owner,
            repo,
            pr.number,
            merge_method="squash",
        )
    except RequestFailed as exc:
        return _handle_squash_failure(exc, pr, action, client, config, owner, repo)

    squash_sha = str(merge_resp.parsed_data.sha)

    # Verify: assert parents[0].sha == pre_squash_develop_sha and confirm a
    # non-empty tree diff (see ``_verify_squash`` for the rationale).
    try:
        _verify_squash(
            client=client,
            owner=owner,
            repo=repo,
            pr=pr,
            pre_squash_develop_sha=pre_squash_develop_sha,
            squash_sha=squash_sha,
        )
    except CorruptSquashError as exc:
        return ActionOutcome(action=action, success=False, error_message=str(exc))

    # Update the status comment to reflect the merged state so PR readers see
    # the final outcome without having to inspect the activation status or
    # cycle summary.
    _upsert_status_comment(
        client,
        owner,
        repo,
        pr,
        state="merged",
        eject_reason=None,
        merged_sha=squash_sha,
    )

    return ActionOutcome(action=action, success=True, error_message=None)


def _handle_squash_failure(
    exc: RequestFailed,
    pr: PRState,
    action: Squash,
    client: GitHubClient,
    config: MergeQueueConfig,
    owner: str,
    repo: str,
) -> ActionOutcome:
    """Translate a merge-API failure into no-op or inline-eject.

    Status-code map (GitHub merge-API docs):

      * **200** — handled by the caller's success path; never reaches here.
      * **405** Method Not Allowed — most common failure. Body inspected:
          - "already merged" → no-op (success=True)
          - "expected" / "in_progress" / "in progress" in body → pending
            required check; do NOT eject, retry next cycle (success=False
            with informational error_message).
          - any other 405 → permanent failure; inline-eject with body text
            as reason.
      * **409** Conflict — head SHA changed since GET (author push raced
        with our squash window). Eject; next cycle re-evaluates if the
        PR is re-enqueued.
      * **422** Unprocessable Entity — merge conflict with develop, or
        validation failure. Eject.
      * other → re-raise (unexpected; orchestrator surfaces as cycle
        failure).
    """
    status = _status_code(exc)
    body = _error_message_body(exc)
    body_lower = body.lower()

    # Idempotent re-merge of a previously-merged PR.
    if status == 405 and "already merged" in body_lower:
        return ActionOutcome(
            action=action,
            success=True,
            error_message="already merged - no-op",
        )

    # Pending required check — don't eject, retry next cycle.
    # Branch protection knows the check exists but it hasn't reported yet.
    _PENDING_MARKERS = ("expected", "in_progress", "in progress")
    if status == 405 and any(m in body_lower for m in _PENDING_MARKERS):
        return ActionOutcome(
            action=action,
            success=False,
            error_message=f"merge pending (required check not yet reported): {body}",
        )

    # Permanent failures — inline-eject with the GitHub-supplied reason.
    # Status 409 = head changed; 422 = unprocessable (typically merge
    # conflict); 405 (other) = required check failed, missing approval, etc.
    if status in (405, 409, 422):
        if status == 409:
            reason = "head SHA changed during squash attempt (author push or rebase)"
        elif status == 422 and "merge conflict" not in body_lower:
            # 422 without explicit conflict wording is still typically a
            # mergeability/validation issue — use the body but prefix for
            # clarity.
            reason = body or "merge unprocessable (validation failed)"
        else:
            # 405 with non-pending message: branch-protection block.
            # GitHub's message is descriptive (e.g., "Required status check
            # 'foo' is failing", "At least 1 approving review is required").
            reason = body or "merge blocked by branch protection"
        _handle_eject(pr, reason, client, config, owner, repo)
        return ActionOutcome(
            action=Eject(pr=pr, reason=reason),
            success=False,
            error_message=f"squash blocked → ejected: {reason}",
        )

    # Unknown / unexpected status — propagate.
    raise exc


def _error_message_body(exc: RequestFailed) -> str:
    """Best-effort extraction of GitHub's ``message`` field from an error.

    GitHub error responses are JSON of the shape
    ``{"message": "...", "documentation_url": "..."}``. Returns the
    ``message`` string when parseable, else the raw response text, else
    ``str(exc)`` as a final fallback. Never raises.
    """
    response = getattr(exc, "response", None)
    if response is not None:
        try:
            data = response.json()
            if isinstance(data, dict) and "message" in data:
                return str(data["message"])
        except (ValueError, AttributeError, TypeError):
            pass
        text = getattr(response, "text", "")
        if text:
            return str(text)
    return str(exc)


def _verify_squash(
    *,
    client: GitHubClient,
    owner: str,
    repo: str,
    pr: PRState,
    pre_squash_develop_sha: str,
    squash_sha: str,
) -> None:
    """Verify a freshly-created squash commit against the Apr-2026
    silent-corruption pattern: a squash that looks correct by parent SHA can
    still produce an empty or identical-to-develop-tip tree.

    Two phases run in sequence; each independently raises ``CorruptSquashError``
    (a ``RuntimeError`` subclass) on failure. ``_handle_squash`` catches the
    exception and returns ``ActionOutcome(success=False)``.

    Phase A — Parent linkage (RFC §4.9):
        Retries ``repos.get_commit(squash_sha)`` up to 3 times on 404 to
        handle read-replication lag (the commit may not yet be visible on
        all replicas). Asserts
        ``commit.parents[0].sha == pre_squash_develop_sha``; a mismatch
        raises with PR number, expected parent, and observed parent.

    Phase B — Tree-diff sanity:
        Calls ``client.rest.repos.compare_commits(basehead=...)`` and
        requires ``status == 'ahead'`` AND ``len(files) > 0`` — a correct
        squash MUST advance develop AND MUST change at least one file. Any
        other shape (``identical``, ``behind``, ``diverged``, or empty
        files) raises ``CorruptSquashError``. ``identical`` and empty-files
        are the literal shapes the Apr-2026 silent-corruption pattern
        produced. Phase B uses the SAME retry budget as Phase A: the API
        resolves the basehead URL by looking up both SHAs, and the same
        replication-visibility lag that motivates Phase A applies here too
        — Phase A's retry may have drained against replica A while Phase
        B's first call hits replica B for the first time.

    TOCTOU caveat on ``pre_squash_develop_sha``:
        The expected parent SHA is captured from
        ``repos.get_branch("develop")`` BEFORE ``pulls.merge``. A cross-job
        advance of develop in that window will still report a false-positive
        ``CorruptSquashError``. The processor's own ``concurrency:
        mq-processor`` block (RFC §4.7) serialises processor cycles; the
        audit job (RFC §4.3.1) re-verifies squash provenance independently
        to defend against the cross-job case.
    """
    commit = _retry_on_404(
        lambda: client.rest.repos.get_commit(owner, repo, squash_sha).parsed_data
    )
    parents = list(commit.parents or [])
    if not parents:
        raise CorruptSquashError(
            f"PR #{pr.number} squash {squash_sha!r}: commit has NO parents; "
            f"expected parent {pre_squash_develop_sha!r}"
        )
    observed_parent = str(parents[0].sha)
    if observed_parent != pre_squash_develop_sha:
        raise CorruptSquashError(
            f"PR #{pr.number} squash {squash_sha!r}: expected parent "
            f"{pre_squash_develop_sha!r}, got {observed_parent!r} "
            "(possible silent corruption in repos.merge_pull)"
        )

    # Phase B: tree-diff sanity. Even with correct parent linkage, the squash
    # commit's tree may be empty or identical to the pre-merge develop tip
    # (the Apr-2026 silent-corruption pattern). compare_commits is the
    # cheapest reliable detector: one API call returns both the relational
    # status and the changed-files list. Same retry budget as Phase A —
    # see docstring for the replica-visibility rationale.
    basehead = f"{pre_squash_develop_sha}...{squash_sha}"
    compare = _retry_on_404(
        lambda: client.rest.repos.compare_commits(
            owner, repo, basehead=basehead
        ).parsed_data
    )
    status = str(getattr(compare, "status", "unknown"))
    files = list(getattr(compare, "files", []) or [])
    if status != "ahead" or not files:
        raise CorruptSquashError(
            f"PR #{pr.number} squash {squash_sha!r}: tree-diff sanity failed "
            f"— status={status!r}, files_count={len(files)} "
            "(expected status='ahead' with non-empty files; matches the "
            "Apr-2026 silent-corruption pattern)"
        )


# ---------------------------------------------------------------------------
# Eject handler
# ---------------------------------------------------------------------------


def _handle_eject(
    pr: PRState,
    reason: str,
    client: GitHubClient,
    config: MergeQueueConfig,
    owner: str,
    repo: str,
) -> ActionOutcome:
    """Overwrite the activation status to ``failure`` and clear mq:* labels.

    Idempotent: status overwrite is unconditional; label removals are wrapped
    in ``_safe_remove_label`` so a previously-removed label does not abort the
    handler mid-eject.

    ``reason`` is passed through to the action record and surfaced in the
    user-facing status comment.
    """
    action = Eject(pr=pr, reason=reason)

    # Overwrite the activation status to failure on the PR's head SHA.
    client.rest.repos.create_commit_status(
        owner,
        repo,
        pr.head_sha,
        state="failure",
        context=config.activation_status_context,
    )

    # Remove all mq:* labels. Iterate over a snapshot so we don't mutate the
    # frozenset during iteration.
    for label in pr.labels:
        if label.startswith(config.label_prefix):
            _safe_remove_label(client, owner, repo, pr.number, label)

    # Upsert a user-facing status comment naming the eject reason — the bare
    # activation-status flip + label removal is invisible in the PR UI.
    _upsert_status_comment(
        client,
        owner,
        repo,
        pr,
        state="ejected",
        eject_reason=reason,
        merged_sha=None,
    )

    return ActionOutcome(action=action, success=True, error_message=None)


def _upsert_status_comment(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr: PRState,
    *,
    state: str,
    eject_reason: str | None,
    merged_sha: str | None,
) -> None:
    """Render + upsert the rocm-mq status comment for a terminal state."""
    import os

    cycle_run_url: str | None = None
    server = os.environ.get("GITHUB_SERVER_URL")
    run_id = os.environ.get("GITHUB_RUN_ID")
    if server and run_id:
        cycle_run_url = f"{server}/{owner}/{repo}/actions/runs/{run_id}"

    ctx = RenderContext(
        author_login="",
        pr_title="",
        queue_positions=(),
        blockers=(),
        cycle_run_url=cycle_run_url,
        state=state,
        eject_reason=eject_reason,
        merged_sha=merged_sha,
    )
    body = render_status_body(pr, ctx, datetime.now(tz=UTC))
    _upsert_comment_body(client, owner, repo, pr.number, body)


def _upsert_comment_body(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    body: str,
) -> None:
    """Find the existing status comment by marker and update; else create."""
    comment_id = _find_status_comment_id(client, owner, repo, pr_number)
    if comment_id is None:
        client.rest.issues.create_comment(owner, repo, pr_number, body=body)
    else:
        client.rest.issues.update_comment(owner, repo, comment_id, body=body)


# ---------------------------------------------------------------------------
# UpdateComment handler
# ---------------------------------------------------------------------------


def _handle_update_comment(
    pr: PRState,
    body: str,
    client: GitHubClient,
    config: MergeQueueConfig,
    owner: str,
    repo: str,
) -> ActionOutcome:
    """Lazily upsert the status comment by ``_STATUS_MARKER`` discovery.

    The marker string must match ``comment._STATUS_MARKER`` byte-for-byte —
    any drift breaks the upsert silently.
    """
    action = UpdateComment(pr=pr, new_body=body)
    _upsert_comment_body(client, owner, repo, pr.number, body)
    return ActionOutcome(action=action, success=True, error_message=None)


_LIST_COMMENTS_PER_PAGE = 100
_LIST_COMMENTS_MAX_PAGES = 50  # safety bound: 5000 comments


def _find_status_comment_id(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
) -> int | None:
    """Scan PR issue comments for ``_STATUS_MARKER`` and return the first id.

    Paginates ``issues.list_comments`` with ``per_page=100``. A single-page
    scan would miss the marker on busy PRs (>30 comments) and silently
    create a duplicate status comment every cycle — at 3-minute cron cadence
    that is 480 duplicates per active PR per day.

    Returns the first matching id while paginating (oldest first; GitHub
    returns comments chronologically), or ``None`` when the marker is never
    seen so the caller creates a fresh comment. The
    ``_LIST_COMMENTS_MAX_PAGES`` safety bound caps the loop so a
    pathological PR with thousands of comments still terminates cleanly.
    """
    for page in range(1, _LIST_COMMENTS_MAX_PAGES + 1):
        resp = client.rest.issues.list_comments(
            owner,
            repo,
            pr_number,
            per_page=_LIST_COMMENTS_PER_PAGE,
            page=page,
        )
        comments = list(resp.parsed_data or [])
        for comment in comments:
            body = str(getattr(comment, "body", "") or "")
            if _STATUS_MARKER in body:
                return int(comment.id)
        # Short page (or empty page) → no more comments to fetch.
        if len(comments) < _LIST_COMMENTS_PER_PAGE:
            return None
    # Hit the safety bound without finding the marker — treat as not-found
    # (the caller will create a new comment; on the next cycle this scan
    # will find that fresh comment within the safety bound).
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_T = TypeVar("_T")


def _retry_on_404(call: Callable[[], _T]) -> _T:
    """Invoke ``call`` with up to ``_VERIFY_SQUASH_RETRIES`` attempts on 404.

    Retries to handle read-replication lag (a just-committed SHA may be
    invisible to the replica handling the follow-up read for ~0.5-2s). We
    back off ``_VERIFY_SQUASH_BACKOFFS`` and retry; any non-404
    ``RequestFailed`` propagates immediately. After the final attempt the
    last 404 propagates (caller cycle aborts; next 3-min cron tick re-runs
    from a fresh snapshot per RFC §4.6).

    Used by both Phase A (``get_commit``) and Phase B (``compare_commits``)
    of ``_verify_squash``; the two share a budget because they race the same
    replication-lag class.
    """
    for attempt in range(_VERIFY_SQUASH_RETRIES):
        try:
            return call()
        except RequestFailed as exc:
            if _status_code(exc) != 404:
                raise
            if attempt == _VERIFY_SQUASH_RETRIES - 1:
                raise
            time.sleep(_VERIFY_SQUASH_BACKOFFS[attempt])
    # Defensive — the loop above either returns or raises. mypy needs this
    # to satisfy the function return type because it cannot prove the loop
    # exhausts via raise.
    raise AssertionError(  # pragma: no cover
        "unreachable: _retry_on_404 loop exited without return/raise"
    )


def _status_code(exc: RequestFailed) -> int | None:
    """Best-effort read of the HTTP status code on a RequestFailed exception.

    The real githubkit RequestFailed carries an ``.response.status_code``; the
    test ``_make_request_failed`` shim mirrors that surface. Return ``None``
    when the attribute path is missing rather than raising — preserves the
    original RequestFailed propagation behaviour for unexpected shapes.
    """
    response = getattr(exc, "response", None)
    if response is None:
        return None
    code = getattr(response, "status_code", None)
    return int(code) if code is not None else None


def _read_pr(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
) -> Any:
    """Read a single PR's parsed_data (carries ``head.sha`` and ``head.ref``)."""
    resp = client.rest.pulls.get(owner, repo, pr_number)
    return resp.parsed_data


def _read_branch_tip(
    client: GitHubClient,
    owner: str,
    repo: str,
    branch: str,
) -> str:
    """Read the current commit SHA at the tip of ``branch``."""
    resp = client.rest.repos.get_branch(owner, repo, branch)
    return str(resp.parsed_data.commit.sha)


def _safe_remove_label(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
    label: str,
) -> None:
    """Remove ``label`` from PR ``pr_number``; swallow 404 (already absent).

    Idempotency contract (RFC §4.6): the stateless processor may retry a
    label removal on the next cycle; if the first attempt succeeded the
    second MUST not crash. ``RequestFailed(404)`` from ``remove_label`` means
    the label is no longer on the PR — exactly the post-state we wanted. All
    other failures propagate so the caller cycle aborts cleanly.
    """
    try:
        client.rest.issues.remove_label(owner, repo, pr_number, label)
    except RequestFailed as exc:
        if _status_code(exc) == 404:
            return
        raise
