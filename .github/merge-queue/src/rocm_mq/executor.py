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
     the SHA we are about to stamp. Runs unconditionally on both 201 and 204
     paths (CR-02); skipping it on 204 left the post-merge → stamp window
     unguarded. If the author has pushed between the merge and the stamp,
     abort with ``ActionOutcome(success=False)`` so a stale SHA never receives
     the activation status. T-02-03-02 mitigation.
  3. Label flip FIRST — ``issues.add_labels([config.active_label])`` then
     ``_safe_remove_label(config.queued_label)`` (404 swallowed via UK-3).
     Recoverable on the next cycle (add_labels is idempotent,
     remove_label swallows 404 — see ``_safe_remove_label``).
  4. Stamp LAST — ``repos.create_commit_status(state="success",
     context=config.activation_status_context)`` on the merged SHA. This
     is the commit point: posting the App's own success status is what
     binds activation per RFC §4.9. Stamping BEFORE the labels were
     flipped would split-brain the PR (status=active, labels=queued)
     on a label-flip failure (CR-03).
- ``_handle_squash`` — IO-05:
  1. Record the develop branch tip via ``repos.get_branch("develop")`` BEFORE
     the squash (this is the value ``_verify_squash`` will assert against).
  2. ``pulls.merge(merge_method="squash")``.
     - RequestFailed(405) → ``ActionOutcome(success=True, "already merged")``.
  3. ``_verify_squash(...)``; ``CorruptSquashError`` → ``ActionOutcome(success=False)``
     carrying the error message. Pitfall 8 / April 2026 incident defence.
- ``_verify_squash`` — composes TWO defences against the Pitfall 8 /
  April 2026 silent-corruption pattern.
  * Phase A (parent linkage): retries ``repos.get_commit(squash_sha)`` up to
    3 times on 404 (read-replication lag, RESEARCH.md UK-4) and asserts
    ``commit.parents[0].sha == pre_squash_develop_sha``; raises
    ``CorruptSquashError`` on mismatch.
  * Phase B (tree-diff sanity, SC#3): calls
    ``repos.compare_commits(basehead=f"{pre_squash_develop_sha}...{squash_sha}")``
    and requires both ``status == 'ahead'`` AND a non-empty ``files`` list;
    ANY other shape (``identical``, ``behind``, ``diverged``, or empty
    files) raises ``CorruptSquashError``. Catches the literal April-2026
    corruption shape where parents look correct but the commit content is
    empty or identical to develop's pre-merge tip.
- ``_handle_eject`` — overwrites the activation status to ``"failure"`` on
  ``pr.head_sha`` and removes every label starting with ``config.label_prefix``
  via ``_safe_remove_label`` (each 404 is swallowed independently).
- ``_handle_update_comment`` — lazily finds the existing status comment via
  ``_find_status_comment_id`` (scanning for the ``_STATUS_MARKER`` literal); if
  found, updates; otherwise creates. The marker MUST match
  ``rocm_mq.comment._STATUS_MARKER`` exactly (T-02-03-06).
- ``_find_status_comment_id`` — paginated ``issues.list_comments`` scan
  (per_page=100, bounded at ``_LIST_COMMENTS_MAX_PAGES`` to cap pathological
  PRs) for the ``<!-- rocm-mq-status -->`` marker; returns the first matching
  id or ``None`` (WR-03). The previous single-page implementation could miss
  the marker on busy PRs and create a duplicate status comment every cycle.
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
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar, assert_never

# PURE-09 positive marker: I/O modules MUST import githubkit at module level so
# the lint (test_io_modules_do_import_githubkit) passes.
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
        # NOTE: this read happens BEFORE the pre-stamp race check below; the
        # race-check re-read below catches any author push that lands between
        # this read and the stamp (CR-02). The previous implementation skipped
        # the race check on the 204 path entirely, leaving an equally-wide
        # window unguarded.
        pr_obj_post = _read_pr(client, owner, repo, pr.number)
        new_sha = str(pr_obj_post.head.sha)
    else:
        new_sha = str(merge_resp.parsed_data.sha)

    # Step 2: pre-stamp race check — always run, on both 201 and 204 paths
    # (CR-02). The race window between determining `new_sha` and calling
    # create_commit_status is identical regardless of merge response: two
    # separate API round-trips. Skipping the check on 204 left author-push
    # races silently misattributing activation to a stale SHA.
    #
    # For 201, `new_sha` is the synthesized merge commit; the post-merge
    # pulls.get re-read should reflect that same SHA as the PR head. A
    # mismatch means the author pushed on top, advancing the head past the
    # merge commit.
    #
    # For 204, `new_sha` is the head as of the post-merge read; this second
    # read catches any push that landed in the (small but real) window
    # between the two pulls.get calls.
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

    # Step 3: label flip FIRST — add mq:active, safely remove mq:queued.
    # These mutations are recoverable on the next cycle: add_labels is
    # idempotent (set semantics) and _safe_remove_label swallows 404
    # (already-absent). Doing the flip BEFORE the stamp ensures that if
    # any of these calls raises, the activation status is NOT yet posted,
    # so the decision layer's is_validly_active check (which binds
    # activation to the App's own status per RFC §4.9) returns False and
    # the next cycle simply re-runs Activate from a clean slate (CR-03).
    #
    # The previous order (stamp → flip) created a split-brain on any
    # add_labels / remove_label failure: status said "active" while
    # labels said "queued", and the decision layer could schedule a
    # Squash for a PR whose human-visible state was still in-queue.
    client.rest.issues.add_labels(
        owner,
        repo,
        pr.number,
        data=[config.active_label],
    )
    _safe_remove_label(client, owner, repo, pr.number, config.queued_label)

    # Step 4: stamp the activation status LAST — this is the commit point.
    # If this call raises, the labels are already flipped but no activation
    # evidence exists; the next cycle's derive_pr will not classify the PR
    # as validly active and will re-Activate (the merge is a 204 no-op, the
    # label adds are idempotent, and this stamp is retried).
    client.rest.repos.create_commit_status(
        owner,
        repo,
        new_sha,
        state="success",
        context=config.activation_status_context,
    )

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

    On merge-API failure, translate GitHub's response into either:
      * **no-op** (idempotent re-merge of an already-merged PR; pending
        required check — wait for next cycle), or
      * **inline eject** (failing required check, missing approval, merge
        conflict, head-SHA-changed-during-attempt).

    Per 03-wr-09: branch protection is the source of truth for required
    checks. The queue no longer pre-evaluates checks before squash; it
    just tries the merge and reads GitHub's error message to decide.

    See ``_verify_squash`` for the Pitfall 8 readback details.
    """
    action = Squash(pr=pr)

    # Step 1: record the develop tip BEFORE the squash. This is the value
    # parents[0].sha MUST equal after a correct squash.
    pre_squash_develop_sha = _read_branch_tip(client, owner, repo, _TRUNK_BRANCH)

    # Step 2: squash-merge. GitHub returns various 4xx codes when the merge
    # is not currently permitted; _handle_squash_failure translates them.
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

    Status-code map (per 03-wr-09 design + GitHub merge-API docs):

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

    # Idempotent re-merge of a previously-merged PR (UK-3).
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
    """Verify a freshly-created squash commit against the Pitfall 8 silent-corruption pattern.

    Two phases run in sequence; each independently raises ``CorruptSquashError``
    (a ``RuntimeError`` subclass) on failure. ``_handle_squash`` catches the
    exception and returns ``ActionOutcome(success=False)``.

    Phase A — Parent linkage (RFC §4.9 / RESEARCH.md UK-4):
        Retries ``repos.get_commit(squash_sha)`` up to 3 times on 404
        (read-replication lag — the squash commit was just created in the
        same call chain but may not yet be visible on a different replica).
        Backoffs are 1s then 2s; any other ``RequestFailed`` propagates
        immediately. Asserts ``commit.parents[0].sha == pre_squash_develop_sha``;
        a mismatch raises with PR number, expected parent, and observed parent
        in the message.

    Phase B — Tree-diff sanity (SC#3 / Pitfall 8 silent-corruption defence):
        Calls ``client.rest.repos.compare_commits(
        basehead=f"{pre_squash_develop_sha}...{squash_sha}")`` and inspects
        ``parsed_data.status`` plus ``parsed_data.files``. The pass condition
        is ``status == 'ahead'`` AND ``len(files) > 0`` — a correct squash
        MUST advance develop AND MUST change at least one file. Any other
        shape raises ``CorruptSquashError``:
          * ``status == 'identical'`` — squash equals pre-merge tip; no
            changes landed. The canonical April-2026 silent-corruption shape.
          * ``files == []`` — empty changed-files list; literal Apr-2026
            silent-corruption shape (status may even appear ``ahead``).
          * ``status in {'behind', 'diverged'}`` — squash failed to advance
            develop cleanly; impossible for a correct squash applied on the
            current tip.
        Phase B uses the SAME retry budget as Phase A. The original Phase B
        implementation argued ``compare_commits`` was immune to replication
        lag because both SHAs are already committed, but that reasoning only
        addressed *content* lag. The same read-replication *visibility* lag
        that motivates Phase A's retry applies to ``compare_commits``: the
        API resolves the ``basehead`` URL fragment by looking up both SHAs,
        and if the replica handling the compare has not yet seen
        ``squash_sha``, the call raises ``RequestFailed(404)``. Phase A's
        retry may complete on attempt 2 or 3 against replica A while Phase
        B's first call hits replica B for the first time. Without a retry
        budget, a correct squash would eject the cycle because one replica
        is half a second behind — exactly the false-positive ejection the
        UK-4 retry was added to prevent (WR-01).

    TOCTOU caveat on ``pre_squash_develop_sha`` (unchanged by Phase B):
        The expected parent SHA is captured from
        ``repos.get_branch("develop")`` BEFORE ``pulls.merge``. A cross-job
        advance of develop in that window will still cause Phase A to report
        a false-positive ``CorruptSquashError``. The processor's own
        ``concurrency: mq-processor`` block (RFC §4.7) serialises processor
        cycles; the Phase 3 audit job (RFC §4.3.1) re-verifies squash
        provenance independently to defend against the cross-job case.
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
            "(Pitfall 8 — possible silent corruption in repos.merge_pull)"
        )

    # ---- Phase B: tree-diff sanity (SC#3 / Pitfall 8 / Apr-2026 incident) ----
    # Even with correct parent linkage, the squash commit's TREE may be
    # corrupted: identical to the pre-merge develop tip (no changes landed)
    # or carrying an empty file list. The Apr-2026 silent-corruption shape
    # presents exactly this way. compare_commits is the cheapest reliable
    # detector: a single API call returns both the relational status
    # ('ahead' | 'behind' | 'identical' | 'diverged') and the changed-files
    # list. Uses the SAME 3x retry budget as Phase A — compare_commits
    # resolves the basehead URL by looking up both SHAs, so the same
    # replication visibility lag class can produce a 404 here even when
    # Phase A's retry already drained against a different replica (WR-01).
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
            "(expected status='ahead' with non-empty files; possible "
            "Pitfall 8 silent corruption per Apr-2026 incident)"
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

    # Upsert a user-facing status comment naming the eject reason. Dogfood
    # drivers (DOG-02/03/05) poll this comment for the reason substring; the
    # bare activation-status flip + label removal is invisible in the PR UI.
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
    """Render + upsert the rocm-mq status comment for a terminal state.

    Builds a minimal RenderContext (only the fields the ejected / merged body
    renderers read are populated; queued/active fields stay empty since this
    helper is only called from terminal processor handlers). The cycle-run URL
    is sourced from ``GITHUB_SERVER_URL`` + ``GITHUB_RUN_ID`` if present so the
    comment links back to the cycle that produced it.
    """
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

    If an existing comment carries the marker, ``update_comment`` it; otherwise
    ``create_comment``. The marker MUST match ``rocm_mq.comment._STATUS_MARKER``
    exactly (T-02-03-06; cross-referenced in both module docstrings).
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

    Paginates ``issues.list_comments`` using page-based iteration with
    ``per_page=100`` (the GitHub API maximum). The previous single-page
    implementation could miss the marker comment on busy PRs (>30
    comments), in which case ``_handle_update_comment`` would create a
    DUPLICATE status comment every cycle — at 3-minute cron cadence that
    is 480 duplicates per active PR per day (WR-03).

    Returns the FIRST matching id encountered while paginating (oldest
    first, since GitHub returns comments in chronological order). If the
    marker is never seen, returns ``None`` and the caller creates a fresh
    comment.

    A ``_LIST_COMMENTS_MAX_PAGES`` safety bound caps the loop so a
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

    The 404 is the read-replication visibility lag described in RESEARCH.md
    UK-4 — a just-committed SHA may be invisible to the replica handling the
    follow-up read for ~0.5-2s. We back off ``_VERIFY_SQUASH_BACKOFFS`` and
    retry; any non-404 ``RequestFailed`` propagates immediately. After the
    final attempt the last 404 propagates (caller cycle aborts; next 3-min
    cron tick re-runs from a fresh snapshot per RFC §4.6).

    Used by both Phase A (``get_commit``) and Phase B (``compare_commits``)
    of ``_verify_squash``; the two share a budget because they race the same
    lag class — see WR-01 in the 02-05 review.
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
