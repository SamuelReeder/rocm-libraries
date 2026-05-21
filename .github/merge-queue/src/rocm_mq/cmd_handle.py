"""
rocm_mq.cmd_handle — Command handler for /merge and /dequeue (RFC §4.5).

Phase 3 plan 03-06 module. Wired by ``cmd_process.run_handle`` (plan 03-01
subparser) and invoked from ``.github/workflows/mq-handler.yml`` (plan 03-07)
on every ``issue_comment: [created]`` event on the fork.

Public surface (Phase 3 contract):
  - ``main(argv: list[str] | None = None) -> int`` — CLI entrypoint.
    Reads $GITHUB_EVENT_PATH (or ``--event-path``), parses the GitHub
    issue_comment webhook payload, dispatches to /merge or /dequeue
    handlers, and returns a process exit code (0 success / non-zero error).
  - ``parse_commands(body: str) -> set[str]`` — per-line regex parser
    (RESEARCH.md Area #6 default).
  - ``is_self_bootstrap(changed_paths: list[str]) -> list[str]`` —
    intersection of changed paths with ``SELF_BOOTSTRAP_PATHS`` (RFC §8
    self-bootstrap protection; RESEARCH.md Area #16).

Layering (PURE-09): this module is I/O layer — it imports ``gh``,
``executor``, ``comment``, ``pathmap``, ``config`` freely plus stdlib
``fnmatch``, ``json``, ``os``, ``re``, ``sys``, ``traceback``. It is NOT in
``PURE_LAYER_MODULES``; ``tests/test_pure_layer_imports.py`` confirms the
boundary.

Token-split discipline (RFC §4.9):
  - The label-apply path, the live perm check, the at-enqueue gate reads,
    pulls.list_files, and the at-enqueue rejection comments go through the
    App installation token (the single ``GITHUB_TOKEN`` env var the workflow
    sets to ``steps.app-token.outputs.token``).
  - The eyes-reaction and the ``<!-- rocm-mq-status -->`` status comment
    are conceptually ``GITHUB_TOKEN``-scoped (RFC §4.9 token split). For
    Phase 3 the handler accepts a single client (the App token) since both
    write surfaces are within the App installation's scoped permissions
    (issues: write covers both label apply AND comment / reaction writes).
    The workflow may pass a distinct GITHUB_TOKEN-backed client in a future
    refactor; the single-client shape today keeps the implementation
    minimal while preserving the audit narrative.

WF coverage:
  - WF-01: live ``repos.get_collaborator_permission_level`` (NEVER
    ``author_association``; Pitfall 17) with the RFC §4.4 PR-author override.
  - WF-02: four at-enqueue gates — ≥1 approving review, no failing required
    check on head SHA, fork maintainer-edits enabled, queue set non-empty.
  - WF-03: idempotent second ``/merge`` — short-circuits to eyes + comment
    upsert when ``mq:queued`` or ``mq:active`` is already on the PR.
  - WF-08 / DOG-08: ``SELF_BOOTSTRAP_PATHS`` rejection BEFORE any state
    mutation.
  - WF-11: status comment upsert via ``executor._find_status_comment_id``
    + ``comment.render_status_body`` (load-bearing ``<!-- rocm-mq-status
    -->`` marker embedded by the renderer).
  - WF-12: NOT directly implemented here — passively satisfied by Phase 2's
    executor (new head SHA after a force-push lacks the App-created
    ``merge-queue/active`` status; the next processor cycle ejects).
    Documented in 03-06-SUMMARY.md.

Architectural note: this module does NOT import ``rocm_mq.decision`` and
does NOT call ``decide_cycle``. ``/merge`` only labels the PR + posts the
status comment + posts the eyes reaction; the processor's next cycle
discovers the queued PR via the standard search-by-label path and runs
the decision algorithm normally (RFC §4.6).
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import sys
import traceback
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from githubkit.exception import RequestFailed

from rocm_mq.comment import render_status_body
from rocm_mq.config import SELF_BOOTSTRAP_PATHS
from rocm_mq.executor import _find_status_comment_id
from rocm_mq.pathmap import queues_for_paths
from rocm_mq.state import MergeQueueConfig, PRState, RenderContext

if TYPE_CHECKING:
    pass


# ---------------------------------------------------------------------------
# Module-level constants
# ---------------------------------------------------------------------------

# Per-line exact-match command regex. Area #6: humans pasting code snippets
# that quote `/merge` in inline-code (backticks at column 1) do NOT match
# because ``^/`` fails after .strip(). Fenced code blocks containing ONLY
# ``/merge`` on a line WOULD match — intentional permissiveness per CONTEXT.md
# Discretion default (option-a; documented in 03-06-SUMMARY.md).
_CMD_RE: re.Pattern[str] = re.compile(r"^/(merge|dequeue)\s*$")

# RFC §4.4: collaborators with these roles may /merge any PR. PR authors are
# always eligible regardless of role (live override in ``_check_perm``).
_ELIGIBLE_ROLES: frozenset[str] = frozenset({"admin", "maintain", "write"})

# RFC §4.3 / D-03 label-name conventions. These mirror the
# ``MergeQueueConfig`` defaults but are kept as module constants here
# because the idempotency short-circuit runs BEFORE the config load, so we
# cannot read them off ``config``.
_LABEL_QUEUED = "mq:queued"
_LABEL_ACTIVE = "mq:active"
_LABEL_PREFIX = "mq:"


# ---------------------------------------------------------------------------
# Public: parse_commands + is_self_bootstrap (independently tested)
# ---------------------------------------------------------------------------


def parse_commands(body: str) -> set[str]:
    """Return the set of recognized commands in a comment body.

    Per-line exact match (RESEARCH.md Area #6 default). Each line is
    stripped of leading/trailing whitespace and matched against
    ``^/(merge|dequeue)\\s*$``. The set return type means duplicate
    commands (``/merge`` posted twice in one comment) collapse to a single
    entry — the handler responds once per distinct command, which matches
    the idempotency contract (RFC §4.5).

    Args:
        body: The full text of an issue comment body.

    Returns:
        A set containing ``"merge"`` and/or ``"dequeue"`` for any lines
        matching the regex; empty set when no recognized command is present.
    """
    return {
        m.group(1)
        for line in body.splitlines()
        if (m := _CMD_RE.match(line.strip()))
    }


def is_self_bootstrap(changed_paths: list[str]) -> list[str]:
    """Return the subset of ``changed_paths`` that intersect SELF_BOOTSTRAP_PATHS.

    Uses ``fnmatch`` (case-sensitive on GitHub's API-returned literal paths;
    Pitfall 12). Symlink / case-only-rename adversarial coverage is Phase 4
    (VAL-03); Phase 3 documents but does not implement those defenses.

    Args:
        changed_paths: List of file paths returned by
            ``pulls.list_files(...).parsed_data[*].filename``.

    Returns:
        Sorted-by-input-order subset of ``changed_paths`` that matched any
        SELF_BOOTSTRAP glob. Empty list means the PR is safe to enqueue
        (from the self-bootstrap perspective).
    """
    hits: list[str] = []
    for path in changed_paths:
        if any(fnmatch.fnmatch(path, pat) for pat in SELF_BOOTSTRAP_PATHS):
            hits.append(path)
    return hits


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

    Implements WF-01. Returns ``(eligible, role_name)``:
      - ``eligible`` is True when the commenter is the PR author (override),
        or when their live ``repos.get_collaborator_permission_level``
        role is in ``_ELIGIBLE_ROLES``.
      - ``role_name`` is the live role string from the API
        (``admin|maintain|write|triage|read|none``), or ``"none"`` when the
        API returns 404 (not a collaborator). The PR-author override path
        also returns the live role if seedable, otherwise ``"none"``.

    NEVER read from ``event.comment.author_association`` — Pitfall 17:
    that field is computed at comment-write time and is not refreshed when
    org / collaborator state changes. The live API call is authoritative.
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
    """Check the four WF-02 at-enqueue gates; return list of failed-gate names.

    Empty return → all four gates passed → handler proceeds with enqueue.
    Non-empty return → at least one gate failed → handler rejects with a
    single comment listing every failing gate (no labels applied).

    Gates (in declaration order; all four are checked even if earlier ones
    fail so the user sees the full picture in one rejection comment):
      1. ``empty-queue-set`` — derived queue set is empty (DOG-08 adjacent;
         this is also handled separately in main() to produce a more
         specific message, but we re-check here for safety).
      2. ``maintainer-edits-disabled`` — ``pr.maintainer_can_modify`` is
         False on a fork PR (the queue's develop-merge push needs this).
      3. ``no-approval`` — ``pulls.list_reviews`` carries zero entries
         whose ``state == "APPROVED"`` (RFC §5: required review state).
      4. ``failing-required-check`` — ``get_combined_status_for_ref`` for
         the PR head SHA reports any context in {"failure", "error"}.
    """
    failed: list[str] = []

    if not queues:
        failed.append("empty-queue-set")

    # The `maintainer_can_modify` field is semantically only meaningful for
    # cross-repo PRs (head and base live in different repositories). For
    # same-repo PRs GitHub returns `false` by default — the field has no
    # real meaning because the PR head IS in the maintainer's repo. Gating
    # on it for same-repo PRs trips drivers that legitimately need to
    # merge through the queue. Restrict the check to cross-repo PRs where
    # the field carries real signal (the fork author must opt-in to let
    # the upstream maintainer push to their branch for the queue's
    # develop-merge step). Discovered live by dog_04 against the fork
    # (same-repo PRs returned `maintainer_can_modify=false` despite the
    # driver explicitly requesting `true`).
    head_repo_id = getattr(
        getattr(getattr(pr, "head", None), "repo", None), "id", None
    )
    base_repo_id = getattr(
        getattr(getattr(pr, "base", None), "repo", None), "id", None
    )
    is_cross_repo = (
        head_repo_id is not None
        and base_repo_id is not None
        and head_repo_id != base_repo_id
    )
    if is_cross_repo and not getattr(pr, "maintainer_can_modify", True):
        failed.append("maintainer-edits-disabled")

    # No-approval gate (RFC §5 / WF-02). Behavior is config-toggled via
    # ``MergeQueueConfig.require_approval_at_enqueue`` so the dogfood fork
    # can disable it (the fork has only one collaborator and PR authors
    # cannot self-approve per RFC §5, blocking every dogfood driver) while
    # the default-True preserves the upstream contract. PORT-02 closure:
    # the config flag replaces a code-level comment-out so the gate cannot
    # silently regress at upstream port time.
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
        if any(getattr(s, "state", "") in {"failure", "error"} for s in per_context):
            failed.append("failing-required-check")

    return failed


def _apply_labels(
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    queues: frozenset[str],
) -> None:
    """Apply ``mq:queued`` + ``mq:<queue>`` for each derived queue.

    Idempotent (the GitHub API + the FakeGitHub semantics 2 contract:
    add_labels on an already-present label is a no-op). One round-trip
    per call — labels are batched in a single request.
    """
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
    """Build a RenderContext for the given state + upsert the status comment.

    Mirrors ``executor._handle_update_comment`` exactly — find the existing
    comment by ``<!-- rocm-mq-status -->`` marker (paginated via WR-03)
    and update it, or create a fresh comment if none exists.
    """
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
    success, no pre-check needed (RESEARCH.md Area #7).
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

    Step order is load-bearing per the plan's behavior block:
      0. WF-12 eyes-reaction — posted FIRST, before any branching. Eyes
         is the ack-receipt signal that the handler saw the comment; it
         is independent of whether the /merge accepts or rejects.
      1. Read PR (need labels for idempotency + author for perm override).
      2. Idempotency short-circuit (mq:queued / mq:active present → eyes
         + comment upsert only; no labels, no perm check, no gates).
      3. Self-bootstrap rejection (RFC §8) — runs BEFORE any state mutation.
      4. Permission check (live API; PR-author override).
      5. Queue derivation (pulls.list_files → pathmap.queues_for_paths).
      6. At-enqueue gates (≥1 approval, no failing required check,
         maintainer-edits, queue set non-empty).
      7. Label apply + status-comment upsert.
    """
    # Step 0 (WF-12): eyes-reaction posted on EVERY received /merge,
    # regardless of accept/reject outcome. Posted FIRST so every downstream
    # rejection branch implicitly carries the ack. The reactions API is
    # idempotent (200 on duplicate, 201 on first) so a second /merge that
    # falls into the idempotency short-circuit below still re-acks safely.
    # Live DOG-04 run 2026-05-20 surfaced the missing-eyes-on-rejection bug
    # (03-wr-05 closes it).
    _post_eyes_reaction(client, owner, repo, comment_id)

    pr_resp = client.rest.pulls.get(owner, repo, pr_number)
    pr = pr_resp.parsed_data
    pr_author_login = str(getattr(getattr(pr, "user", None), "login", ""))
    current_labels = {str(getattr(lbl, "name", "")) for lbl in getattr(pr, "labels", [])}

    # Step 2: idempotency short-circuit (WF-03). Eyes already posted above.
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

    # Step 2: self-bootstrap rejection (DOG-08 / RFC §8). NO state mutation
    # on rejection — no labels, no eyes, no status comment.
    files_resp = client.rest.pulls.list_files(owner, repo, pr_number)
    changed_paths_list: list[str] = [
        str(f.filename) for f in (files_resp.parsed_data or [])
    ]
    bootstrap_hits = is_self_bootstrap(changed_paths_list)
    if bootstrap_hits:
        body = (
            "/merge cannot enqueue this PR because it modifies merge-queue "
            f"infrastructure paths: {', '.join(f'`{p}`' for p in bootstrap_hits)}. "
            "RFC §8 (self-bootstrap protection) requires manual maintainer review "
            "for changes to these paths. Once reviewed, a maintainer can merge "
            "directly via the GitHub UI."
        )
        _post_comment(client, owner, repo, pr_number, body)
        return 0

    # Step 3: permission check (WF-01 — live API; NEVER author_association).
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
            "author. Per RFC §4.4 the merge queue requires write-or-above "
            "permission to enqueue another author's PR."
        )
        _post_comment(client, owner, repo, pr_number, body)
        return 0

    # Step 4: derive the per-PR queue set via the RFC §4.2 pathmap.
    changed_paths = tuple(changed_paths_list)
    queues = queues_for_paths(changed_paths, config)

    # DOG-08 (no opted-in path) — single, more specific comment than the
    # generic gate failure message.
    if not queues:
        body = (
            "`/merge` rejected: this PR touches no opted-in queue paths "
            "(per `.github/merge-queue/path_to_queues.yml`). Only PRs whose "
            "changes land under an opted-in path tree can be enqueued. "
            "Maintainers may merge non-opted-in changes via the GitHub UI."
        )
        _post_comment(client, owner, repo, pr_number, body)
        return 0

    # Step 5: at-enqueue gates (WF-02).
    failed_gates = _check_at_enqueue_gates(
        client, owner, repo, pr_number, pr, queues,
        require_approval=config.require_approval_at_enqueue,
    )
    if failed_gates:
        body = (
            "`/merge` rejected: the following at-enqueue gates failed: "
            f"{', '.join(f'`{g}`' for g in failed_gates)}. Re-post `/merge` "
            "once each condition is addressed (RFC §4.3 / WF-02)."
        )
        _post_comment(client, owner, repo, pr_number, body)
        return 0

    # Step 7: success — label apply, status comment. Eyes already posted
    # at Step 0 (WF-12 ack-receipt) so we don't double-post here.
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

    Currently does NOT re-run the at-enqueue perm check on dequeue —
    anyone who can comment on the PR may dequeue it (RFC §4.5 is silent
    on the exact perm boundary; Phase 4 may tighten this). Eyes-reaction
    is posted as the visible ack; status comment is upserted to an
    "ejected" state with reason "/dequeue requested".
    """
    _ = commenter_login  # reserved for Phase 4 perm tightening
    pr_resp = client.rest.pulls.get(owner, repo, pr_number)
    pr = pr_resp.parsed_data
    pr_author_login = str(getattr(getattr(pr, "user", None), "login", ""))

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
            "Phase 3 plan 03-06 command handler — dispatches /merge and "
            "/dequeue from a GitHub issue_comment webhook payload."
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
        rejection, DOG-08 rejection, non-/merge comment skip,
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
        # Preserve the full traceback — WR-04: operators debugging a
        # production failure need module:line attribution, not just
        # repr(exc).
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
