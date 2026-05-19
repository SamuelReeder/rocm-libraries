"""
rocm_mq.executor — Action dispatcher; executes GitHub mutations for each Action
produced by ``decide_cycle`` (IO-03, IO-04, IO-05).

PURE-09 compliance statement: this module is NOT in PURE_LAYER_MODULES; it
intentionally imports githubkit (via ``rocm_mq.gh.GitHubClient`` and
``githubkit.exception`` for narrow except clauses). Inert ``Action`` data flows
in from the pure decision layer; only this module turns those into mutations.

Public surface:
- ``dispatch(action, client, config, owner, repo) -> ActionOutcome`` —
  exhaustive ``match`` over the five Action variants with
  ``case _: assert_never(action)`` so a future variant addition fails at lint
  time. Each variant routes to its private ``_handle_*`` helper or, for
  ``Defer``, returns ``ActionOutcome(success=True)`` without any API call.

Private helpers (exposed for unit tests):
- ``_handle_activate`` — RFC §4.9 activation state machine:
  1. ``repos.merge(base=<PR head ref>, head="develop")`` — pull develop into
     the PR branch.
     - 201 Created → record ``response.parsed_data.sha`` as the post-merge tip.
     - 204 No Content → no-op (develop already in PR); fall back to the PR's
       current head SHA via a ``pulls.get`` re-read.
     - RequestFailed(409) → return ``ActionOutcome(success=False)`` with reason
       ``"merge conflict with develop"``; no stamp, no label flip.
  2. Pre-stamp race check — re-read ``pulls.get`` and compare ``head.sha`` to
     the SHA we are about to stamp. If the author has pushed between the merge
     and the stamp, abort with ``ActionOutcome(success=False)`` so a stale SHA
     never receives the activation status. T-02-03-02 mitigation.
  3. Stamp — ``repos.create_commit_status(state="success",
     context=config.activation_status_context)`` on the merged SHA.
  4. Label flip — ``issues.add_labels([config.active_label])`` then
     ``_safe_remove_label(config.queued_label)`` (404 swallowed via UK-3).
- ``_handle_squash`` — IO-05:
  1. Record the develop branch tip via ``repos.get_branch("develop")`` BEFORE
     the squash (this is the value ``_verify_squash`` will assert against).
  2. ``pulls.merge(merge_method="squash")``.
     - RequestFailed(405) → ``ActionOutcome(success=True, "already merged")``.
  3. ``_verify_squash(...)``; ``CorruptSquashError`` → ``ActionOutcome(success=False)``
     carrying the error message. Pitfall 8 / April 2026 incident defence.
- ``_verify_squash`` — retries ``repos.get_commit(squash_sha)`` up to 3 times
  on 404 (read-replication lag, RESEARCH.md UK-4). Asserts
  ``commit.parents[0].sha == pre_squash_develop_sha`` and raises
  ``CorruptSquashError`` on mismatch.
- ``_handle_eject`` — overwrites the activation status to ``"failure"`` on
  ``pr.head_sha`` and removes every label starting with ``config.label_prefix``
  via ``_safe_remove_label`` (each 404 is swallowed independently).
- ``_handle_update_comment`` — lazily finds the existing status comment via
  ``_find_status_comment_id`` (scanning for the ``_STATUS_MARKER`` literal); if
  found, updates; otherwise creates. The marker MUST match
  ``rocm_mq.comment._STATUS_MARKER`` exactly (T-02-03-06).
- ``_find_status_comment_id`` — single-page ``issues.list_comments`` scan for
  the ``<!-- rocm-mq-status -->`` marker; returns the first matching id or
  ``None``. Pagination is a known limitation (see Phase 3 follow-up).
- ``_safe_remove_label`` — wraps ``issues.remove_label`` and swallows
  RequestFailed(404) (label already absent; idempotent no-op per RFC §4.6).

Constants:
- ``_STATUS_MARKER`` — MUST match the literal in ``rocm_mq.comment``. The marker
  is what ``_find_status_comment_id`` greps for; the renderer puts it in every
  rendered body. Drift between the two files breaks upsert silently.
- ``_TRUNK_BRANCH`` — the merge-queue trunk branch name; ``"develop"`` per RFC
  §4.9. Kept as a module-level constant so a hypothetical fork on a different
  trunk can override via monkeypatch.
- ``_VERIFY_SQUASH_RETRIES`` / ``_VERIFY_SQUASH_BACKOFF_SECONDS`` — 3 attempts,
  1s then 2s backoff between get_commit retries (UK-4 replication lag).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, assert_never

# PURE-09 positive marker: I/O modules MUST import githubkit at module level so
# the lint (test_io_modules_do_import_githubkit) passes.
from githubkit.exception import RequestFailed

from rocm_mq.gh import CorruptSquashError
from rocm_mq.state import (
    Action,
    ActionOutcome,
    Activate,
    Defer,
    Eject,
    MergeQueueConfig,
    PRState,
    Squash,
    UpdateComment,
)

if TYPE_CHECKING:
    from rocm_mq.gh import GitHubClient


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# MUST stay byte-identical to rocm_mq.comment._STATUS_MARKER. The renderer
# embeds this in every status comment body; _find_status_comment_id greps for
# this exact substring to locate the existing comment. Drift = silent upsert
# breakage (T-02-03-06).
_STATUS_MARKER = "<!-- rocm-mq-status -->"

# Trunk branch name per RFC §4.9. Not in MergeQueueConfig because the federated
# merge queue's design pins this to "develop" by contract.
_TRUNK_BRANCH = "develop"

# Post-squash readback retry budget — RESEARCH.md UK-4 (read-replication lag).
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

    See module docstring for the state-machine recipe. The pre-stamp race
    check defends against an author pushing between the merge and the stamp:
    if the head SHA we are about to stamp is not the PR's current head, abort
    rather than mark a stale SHA as active (T-02-03-02).
    """
    action = Activate(pr=pr)

    # Step 1: pull develop into the PR branch. The base is the PR's head ref
    # (the branch we want to merge develop INTO); the head is "develop". We
    # read the PR's head ref via pulls.get (PRState doesn't carry the branch
    # name — see 02-03-SUMMARY for the design choice).
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
        # 409 = merge conflict; eject-worthy but we let the caller decide.
        if _status_code(exc) == 409:
            return ActionOutcome(
                action=action,
                success=False,
                error_message="merge conflict with develop",
            )
        raise

    # Determine the SHA we will stamp.
    if getattr(merge_resp, "status_code", None) == 204 or merge_resp.parsed_data is None:
        # 204: develop already contained in the PR branch; nothing was created.
        # Fall back to the PR's CURRENT head SHA (already-up-to-date case).
        pr_obj_post = _read_pr(client, owner, repo, pr.number)
        new_sha = str(pr_obj_post.head.sha)
    else:
        new_sha = str(merge_resp.parsed_data.sha)

    # Step 2: pre-stamp race check. Re-read the PR head SHA and compare. The
    # 204 path already re-read (and there is no race window between the
    # re-read and the stamp), but the 201 path MUST re-read here to catch a
    # push that landed between the merge and our stamp call.
    pr_obj_check = _read_pr(client, owner, repo, pr.number)
    current_head = str(pr_obj_check.head.sha)
    # In the 201 case, current_head should equal new_sha ONLY if the author
    # pushed-on-top in a way that produced the same SHA — vanishingly unlikely.
    # In practice: 201 → new_sha is a merge commit on the PR branch, so
    # current_head SHOULD equal new_sha. Mismatch = author pushed.
    # In the 204 case, we already used current_head as new_sha so this is a
    # tautology.
    if getattr(merge_resp, "status_code", None) != 204 and current_head != new_sha:
        return ActionOutcome(
            action=action,
            success=False,
            error_message=(
                f"pre-stamp race: PR #{pr.number} head advanced from "
                f"{new_sha!r} to {current_head!r} between develop merge and stamp"
            ),
        )

    # Step 3: stamp the activation status.
    client.rest.repos.create_commit_status(
        owner,
        repo,
        new_sha,
        state="success",
        context=config.activation_status_context,
    )

    # Step 4: label flip — add mq:active, safely remove mq:queued.
    client.rest.issues.add_labels(
        owner,
        repo,
        pr.number,
        data=[config.active_label],
    )
    _safe_remove_label(client, owner, repo, pr.number, config.queued_label)

    return ActionOutcome(action=action, success=True, error_message=None)


# ---------------------------------------------------------------------------
# Squash handler — IO-05 / Pitfall 8 defence
# ---------------------------------------------------------------------------


def _handle_squash(
    pr: PRState,
    client: GitHubClient,
    config: MergeQueueConfig,
    owner: str,
    repo: str,
) -> ActionOutcome:
    """Squash-merge ``pr`` and verify the post-squash parent SHA.

    See ``_verify_squash`` for the Pitfall 8 readback details.
    """
    action = Squash(pr=pr)

    # Step 1: record the develop tip BEFORE the squash. This is the value
    # parents[0].sha MUST equal after a correct squash.
    pre_squash_develop_sha = _read_branch_tip(client, owner, repo, _TRUNK_BRANCH)

    # Step 2: squash-merge. 405 = already merged (idempotent no-op per UK-3).
    try:
        merge_resp = client.rest.pulls.merge(
            owner,
            repo,
            pr.number,
            merge_method="squash",
        )
    except RequestFailed as exc:
        if _status_code(exc) == 405:
            return ActionOutcome(
                action=action,
                success=True,
                error_message="already merged - no-op",
            )
        raise

    squash_sha = str(merge_resp.parsed_data.sha)

    # Step 3: verify. Pitfall 8 — assert parents[0].sha == pre_squash_develop_sha.
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

    return ActionOutcome(action=action, success=True, error_message=None)


def _verify_squash(
    *,
    client: GitHubClient,
    owner: str,
    repo: str,
    pr: PRState,
    pre_squash_develop_sha: str,
    squash_sha: str,
) -> None:
    """Read ``squash_sha`` and assert its first parent matches the pre-squash develop tip.

    Retries on 404 to absorb read-replication lag (RESEARCH.md UK-4): the
    squash commit was just created by the same call chain, but the SHA may
    not yet be visible to ``repos.get_commit`` on a different replica. We
    retry up to 3 times with 1s and 2s backoffs. Any other ``RequestFailed``
    propagates immediately (not a known transient).

    On a mismatch, raises ``CorruptSquashError`` (RuntimeError subclass) with
    a descriptive message naming the PR number, the squash SHA, the expected
    parent, and the observed parent — Pitfall 8 / April 2026 incident defence.
    """
    last_exc: RequestFailed | None = None
    commit: Any | None = None
    for attempt in range(_VERIFY_SQUASH_RETRIES):
        try:
            resp = client.rest.repos.get_commit(owner, repo, squash_sha)
            commit = resp.parsed_data
            break
        except RequestFailed as exc:
            if _status_code(exc) != 404:
                raise
            last_exc = exc
            if attempt == _VERIFY_SQUASH_RETRIES - 1:
                # Out of retries — propagate the last 404.
                raise
            time.sleep(_VERIFY_SQUASH_BACKOFFS[attempt])
    else:  # pragma: no cover  (defensive — the loop always breaks or raises)
        raise last_exc  # type: ignore[misc]

    assert commit is not None  # narrowed for mypy/type checkers
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
            "(Pitfall 8 — possible silent corruption in repos.merge_pull)"
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
    handler mid-eject (T-02-03-04).

    ``reason`` is passed through to the action record but is not currently
    posted as a status description — that surface is the responsibility of the
    status comment (rendered separately).
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

    return ActionOutcome(action=action, success=True, error_message=None)


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

    If an existing comment carries the marker, ``update_comment`` it; otherwise
    ``create_comment``. The marker MUST match ``rocm_mq.comment._STATUS_MARKER``
    exactly (T-02-03-06; cross-referenced in both module docstrings).
    """
    action = UpdateComment(pr=pr, new_body=body)
    comment_id = _find_status_comment_id(client, owner, repo, pr.number)
    if comment_id is None:
        client.rest.issues.create_comment(
            owner,
            repo,
            pr.number,
            body=body,
        )
    else:
        client.rest.issues.update_comment(
            owner,
            repo,
            comment_id,
            body=body,
        )
    return ActionOutcome(action=action, success=True, error_message=None)


def _find_status_comment_id(
    client: GitHubClient,
    owner: str,
    repo: str,
    pr_number: int,
) -> int | None:
    """Scan PR issue comments for ``_STATUS_MARKER`` and return the first id.

    Single-page implementation (Phase 3 will paginate if multiple status
    comments per PR ever appear in practice). Returns ``None`` if no comment
    contains the marker.
    """
    resp = client.rest.issues.list_comments(owner, repo, pr_number)
    comments = resp.parsed_data or []
    for comment in comments:
        body = str(getattr(comment, "body", "") or "")
        if _STATUS_MARKER in body:
            return int(comment.id)
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


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

    Idempotency contract (UK-3 / RFC §4.6): the stateless processor may retry
    a label removal on the next cycle; if the first attempt succeeded the
    second attempt MUST not crash. ``RequestFailed(404)`` from ``remove_label``
    means the label is no longer on the PR — exactly the post-state we wanted.
    All other failures propagate so the caller cycle aborts cleanly.
    """
    try:
        client.rest.issues.remove_label(owner, repo, pr_number, label)
    except RequestFailed as exc:
        if _status_code(exc) == 404:
            return
        raise
