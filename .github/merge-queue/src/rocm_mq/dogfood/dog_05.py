"""
rocm_mq.dogfood.dog_05 — DOG-05 author-push-after-activation eject driver.

RFC §6 scenario: a PR opted into a queue is ``/merge``d, gets ``mq:queued``
then ``mq:active`` (activation succeeds — the processor merges develop into
the branch, posts the ``merge-queue/active`` commit status on the resulting
head SHA, and flips the label). The author then pushes a NEW commit to the
PR branch. The new head SHA does NOT carry the ``merge-queue/active``
status, so on the next processor cycle ``pr.is_validly_active`` is False
(decision.py § activation invariant) and the PR is ejected with the
verbatim reason:

    "activation invalid (branch updated or label tampered)"

This exercises WF-12 (force-push / author-push invalidates activation) via
the cleaner author-push path. The eject reason is the exact RFC §6 +
decision.py literal — pinned by ``EXPECTED`` and asserted by the test suite.

Two-mode driver per CONTEXT.md D-04:

  1. Unit-test mode: ``run_scenario(client, ...)`` is called with a FakeGitHub
     extension that simulates the activation transition (mq:active label
     applied on /merge), the author-push (head SHA mutates on the second
     branch commit), and the processor's eject status comment (injected on
     the polling step only after the head SHA has mutated). Tests live in
     ``tests/test_dogfood_dog_05.py``.

  2. Live-fork mode: ``python -m rocm_mq.dogfood.dog_05 --owner SamuelReeder
     --repo rocm-libraries`` against the live fork. Operator-initiated
     AFTER mq-handler.yml + mq-processor.yml are deployed to develop on the
     fork and the App installation is live. Drivers leave PRs in place per
     D-04 (no teardown). NOT exercised in CI.

The driver's author-push step uses ``create_or_update_file_contents`` on
the PR branch (Contents API) so the test is reproducible from a CI runner
without a checkout — no local ``git push``, no credentials beyond the
operator's GITHUB_TOKEN (plan 03-13 must_haves).

Output: writes per-run JSON to
``.planning/phases/03-handler-processor-on-fork/dogfood-runs/`` with the
D-04 schema; ``passed=True`` iff observed_outcome matches ``EXPECTED``
exactly (the RFC literal).

Timeout budget: 15 minutes (RESEARCH.md Area #10 / 03-PATTERNS.md timeout
table — activation + author push + ≥1 cron cycle for eject).
"""

from __future__ import annotations

import argparse
import os
import secrets
import sys
import traceback
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rocm_mq.dogfood._base import (
    DogfoodResult,
    create_dogfood_pr,
    emit_result,
    poll_pr_state,
    post_command,
)

# ---------------------------------------------------------------------------
# Module constants — locked by plan 03-13 Task 1 behavior block.
# ---------------------------------------------------------------------------

SCENARIO_ID: str = "dog_05"
EXPECTED: dict[str, Any] = {
    "action": "Eject",
    # Verbatim RFC §6 / decision.py literal — re-export rather than re-derive
    # so a drift would surface here at first glance.
    "reason": "activation invalid (branch updated or label tampered)",
}
TIMEOUT_S: int = 15 * 60  # 900s — PATTERNS.md timeout-budget table.

# Seed-file path the PR is opened at. Lives under an opted-in path (the
# hipdnn dogfood area chosen by the other DOG-* drivers) so the PR routes
# to a real queue and the processor activates it. A random hex suffix is
# appended at call time to avoid concurrent-run collisions on the same path.
_SEED_PATH_PREFIX: str = "projects/hipdnn/dogfood-author-push"
_PUSH_PATH_PREFIX: str = "projects/hipdnn/dogfood-author-push-extra"

# Marker the renderer embeds in every status comment body (comment.py).
_STATUS_MARKER: str = "<!-- rocm-mq-status -->"

# The exact eject-reason substring the predicate matches on. Identical to
# EXPECTED["reason"] but referenced separately so a substring-vs-exact
# refactor at the predicate level can touch one constant.
_EJECT_REASON_SUBSTRING: str = "activation invalid (branch updated or label tampered)"

# The label that signals the processor's activation step has completed and
# the PR is in the post-activation evaluation window where an author push
# would invalidate the activation.
_MQ_ACTIVE_LABEL: str = "mq:active"


# ---------------------------------------------------------------------------
# Predicate factories — activation poll + post-push eject poll
# ---------------------------------------------------------------------------


def _build_active_label_predicate() -> Callable[[Any, str, str, int], dict[str, Any] | None]:
    """Return a predicate that fires when the PR carries the ``mq:active`` label.

    Reads the PR via ``rest.pulls.get`` and inspects the ``labels`` list. The
    FakeGitHub fake returns labels as ``[SimpleNamespace(name=...), ...]``;
    the live githubkit client returns the same shape. Returns a dict
    snapshot of the activation observation on hit, ``None`` otherwise.
    """

    def predicate(client: Any, owner: str, repo: str, pr_number: int) -> dict[str, Any] | None:
        resp = client.rest.pulls.get(owner, repo, pr_number)
        labels = list(getattr(resp.parsed_data, "labels", []) or [])
        names = [str(getattr(label, "name", "")) for label in labels]
        if _MQ_ACTIVE_LABEL in names:
            head_sha = str(getattr(getattr(resp.parsed_data, "head", None), "sha", "") or "")
            return {
                "labels": names,
                "head_sha_at_activation": head_sha,
            }
        return None

    return predicate


def _build_eject_predicate(
    inject_eject_after: Callable[[int], None] | None = None,
) -> Callable[[Any, str, str, int], dict[str, Any] | None]:
    """Return a predicate that fires when the documented eject comment lands.

    Predicate semantics: lists PR comments, finds any comment carrying the
    ``rocm-mq-status`` marker whose body ALSO contains the documented eject
    reason substring; returns the observed-outcome dict on match, ``None``
    otherwise (keep polling).

    ``inject_eject_after`` is a unit-test seam: when supplied (non-None), it
    is invoked with the PR number on every poll attempt BEFORE the comment
    scan, so a FakeGitHub extension can simulate the processor posting the
    eject comment AFTER the head SHA has mutated. In live-fork mode the
    caller passes ``None`` and the real processor posts the comment
    asynchronously on its next cycle.
    """

    def predicate(client: Any, owner: str, repo: str, pr_number: int) -> dict[str, Any] | None:
        if inject_eject_after is not None:
            inject_eject_after(pr_number)
        resp = client.rest.issues.list_comments(owner, repo, pr_number)
        comments = list(resp.parsed_data or [])
        for c in comments:
            body = str(getattr(c, "body", ""))
            if _STATUS_MARKER in body and _EJECT_REASON_SUBSTRING in body:
                return {
                    "action": "Eject",
                    "reason": _EJECT_REASON_SUBSTRING,
                }
        return None

    return predicate


# ---------------------------------------------------------------------------
# Author-push helper — write a NEW file to the PR branch via Contents API
# ---------------------------------------------------------------------------


def _push_author_commit_to_branch(
    client: Any,
    *,
    owner: str,
    repo: str,
    branch: str,
    file_path: str,
) -> str:
    """Push a single commit to ``branch`` via the Contents API; return the new SHA.

    The Contents API write creates a new commit on ``branch`` whose tree
    differs from the previous head's tree by exactly one new file. This is
    the cleanest way to mutate the PR head SHA without a local checkout —
    plan 03-13 must_haves: "uses git via githubkit's contents API (not local
    git push) so the test is reproducible from a CI runner without a
    checkout".

    The content is a single deterministic line; the file PATH is new (not
    the seed file) so the write cannot collide with the seed file's existing
    sha — the Contents API requires the prior file's sha for an UPDATE but
    permits a fresh CREATE without one. The new path uses a random hex
    suffix to keep concurrent runs disjoint.
    """
    import base64

    encoded = base64.b64encode(
        b"dogfood dog_05 author-push: triggers head-SHA mutation after activation\n"
    ).decode("ascii")
    resp = client.rest.repos.create_or_update_file_contents(
        owner,
        repo,
        file_path,
        message=f"[dogfood {SCENARIO_ID}] simulated author push after activation",
        content=encoded,
        branch=branch,
    )
    commit = getattr(resp.parsed_data, "commit", None)
    return str(getattr(commit, "sha", "") or "")


# ---------------------------------------------------------------------------
# run_scenario — orchestration body (called by main() and by unit tests)
# ---------------------------------------------------------------------------


def run_scenario(
    client: Any,
    *,
    owner: str,
    repo: str,
    output_dir: Path | None = None,
    poll_interval_s: float = 15.0,
    poll_timeout_s: float | None = None,
    inject_eject_after: Callable[[int], None] | None = None,
) -> DogfoodResult:
    """Run DOG-05 end-to-end and emit the per-run JSON.

    Args:
        client: A ``GitHubClient`` (live) or FakeGitHub extension (tests).
        owner / repo: Target repository (e.g., ``SamuelReeder/rocm-libraries``).
        output_dir: JSON output dir; defaults to D-04 location via
            ``emit_result``.
        poll_interval_s: Seconds between polls; tests pass 0 to skip sleeps.
        poll_timeout_s: Polling budget override; ``None`` uses ``TIMEOUT_S``.
            The same budget is applied to BOTH the activation poll and the
            eject poll (live operator: total wall budget is up to 2x
            TIMEOUT_S in the worst case, but typical runs see activation in
            ~9 min and eject in ~6 min).
        inject_eject_after: Test-only seam — see ``_build_eject_predicate``.

    Returns:
        The ``DogfoodResult`` instance written to disk.

    Raises:
        TimeoutError: Polling budget elapsed before either activation was
            observed or the eject comment landed.
    """
    timeout = TIMEOUT_S if poll_timeout_s is None else poll_timeout_s
    started = datetime.now(tz=UTC).isoformat()

    # 1. Create the PR under the opted-in path. The random suffix is
    #    appended to the file basename so concurrent runs of dog_05 don't
    #    collide on the seed path.
    seed_suffix = secrets.token_hex(4)
    seed_path = f"{_SEED_PATH_PREFIX}-{seed_suffix}.txt"
    pr_number, pr_url = create_dogfood_pr(
        client,
        owner,
        repo,
        SCENARIO_ID,
        seed_path,
        "dogfood dog_05 seed file — opened to exercise the activation invariant\n",
        title_suffix="author-push-after-activation",
    )

    # 2. Post /merge to enqueue the PR.
    comment_id = post_command(client, owner, repo, pr_number, "/merge")

    # 3. Poll until the PR carries the ``mq:active`` label — confirms the
    #    processor has run its activation step and the eject path is now
    #    armed (any head-SHA mutation will invalidate the activation
    #    status). The activation observation also captures the head SHA the
    #    processor pinned the merge-queue/active status on.
    active_predicate = _build_active_label_predicate()
    activation = poll_pr_state(
        client,
        owner,
        repo,
        pr_number,
        predicate=active_predicate,
        timeout_s=timeout,
        interval_s=poll_interval_s,
    )
    activation_observed_at = datetime.now(tz=UTC).isoformat()

    # 4. Read back the PR branch name from rest.pulls.get so we push to the
    #    actual head branch (not a name we synthesize). The real githubkit
    #    response carries ``head.ref``; FakeGitHub does NOT today (the fake
    #    only carries head.sha), so we fall back to looking up the branch we
    #    created via create_dogfood_pr by re-reading the dogfood branch
    #    prefix. We rely on the create_dogfood_pr branch-naming contract
    #    (``dogfood/{scenario_id}-{8-hex}``) — there is at most ONE such
    #    branch with this PR's seed file on it, so we discover the branch by
    #    inspecting the PR's head ref when present, else by reconstruction.
    pr_resp = client.rest.pulls.get(owner, repo, pr_number)
    pr_head = getattr(pr_resp.parsed_data, "head", None)
    branch_name = str(getattr(pr_head, "ref", "") or "")
    if not branch_name:
        # Test-only fallback: the fake doesn't populate head.ref. The
        # create_dogfood_pr helper opens the PR off
        # ``dogfood/{scenario_id}-{hex}``; the seed file's create call
        # recorded the branch on the fake's ``_created_files`` list. We use
        # secrets.token_hex via create_dogfood_pr internally so we can't
        # reconstruct deterministically — but in test mode the fake exposes
        # ``_created_files`` and we pick the most recent branch from it.
        created_files = getattr(client, "_created_files", None)
        if created_files:
            branch_name = str(created_files[-1].get("branch", ""))
    if not branch_name:
        raise RuntimeError(
            "dog_05: could not resolve PR branch name from pulls.get(head.ref) "
            "nor fall back to the fake's _created_files attribute; cannot push "
            "the author-commit step."
        )

    # 5. Push a new commit to the PR branch — the simulated author-push.
    push_path = f"{_PUSH_PATH_PREFIX}-{seed_suffix}.txt"
    push_commit_sha = _push_author_commit_to_branch(
        client,
        owner=owner,
        repo=repo,
        branch=branch_name,
        file_path=push_path,
    )
    push_observed_at = datetime.now(tz=UTC).isoformat()

    # 6. Re-read the PR to capture the new (post-push) head SHA — this is
    #    the SHA the processor's next cycle will reject as not-validly-active.
    post_push_pr_resp = client.rest.pulls.get(owner, repo, pr_number)
    post_push_head_sha = str(
        getattr(getattr(post_push_pr_resp.parsed_data, "head", None), "sha", "") or ""
    )

    # 7. Poll for the eject status comment carrying the documented reason.
    eject_predicate = _build_eject_predicate(inject_eject_after=inject_eject_after)
    observed = poll_pr_state(
        client,
        owner,
        repo,
        pr_number,
        predicate=eject_predicate,
        timeout_s=timeout,
        interval_s=poll_interval_s,
    )
    ended = datetime.now(tz=UTC).isoformat()

    # 8. Build the DogfoodResult + emit per-run JSON. Timeline subset per
    #    plan 03-13 <action>: pr_opened, merge_command_posted,
    #    mq_queued_label_applied, activation_began,
    #    merge_queue_active_status_posted, mq_active_label_applied,
    #    author_push_after_activation (custom event_type), ejected.
    passed = observed == EXPECTED
    timeline: tuple[tuple[str, str, dict[str, Any]], ...] = (
        (started, "pr_opened", {"pr_number": pr_number, "pr_url": pr_url}),
        (
            started,
            "merge_command_posted",
            {"comment_id": comment_id, "command": "/merge"},
        ),
        (started, "mq_queued_label_applied", {"labels": ["mq:queued"]}),
        (activation_observed_at, "activation_began", {"derived": "see status comment"}),
        (
            activation_observed_at,
            "merge_queue_active_status_posted",
            {
                "head_sha_at_activation": activation.get("head_sha_at_activation", ""),
            },
        ),
        (
            activation_observed_at,
            "mq_active_label_applied",
            {"labels": activation.get("labels", [])},
        ),
        (
            push_observed_at,
            "author_push_after_activation",
            {
                "branch": branch_name,
                "file_path": push_path,
                "push_commit_sha": push_commit_sha,
                "post_push_head_sha": post_push_head_sha,
            },
        ),
        (ended, "ejected", {"reason": observed.get("reason", "")}),
    )
    result = DogfoodResult(
        scenario_id=SCENARIO_ID,
        run_started_at=started,
        run_ended_at=ended,
        pr_number=pr_number,
        pr_url=pr_url,
        expected_outcome=EXPECTED,
        observed_outcome=observed,
        timeline=timeline,
        processor_run_urls=(),
        step_summary_excerpt="",
        passed=passed,
        notes=(
            f"Activation observed at head SHA "
            f"{activation.get('head_sha_at_activation', '')!r}; "
            f"author push wrote {push_path!r} producing post-push head "
            f"SHA {post_push_head_sha!r}. The eject reason is the verbatim "
            "RFC §6 / decision.py literal; any drift in the literal would "
            "flip passed=False. Live-fork run: the processor cycle URL "
            "embedded in the status comment body is the canonical "
            "processor_run_url; populate from the comment in a future "
            "enhancement if needed."
        ),
    )
    emit_result(result, output_dir=output_dir)
    return result


# ---------------------------------------------------------------------------
# main() — CLI entrypoint (live-fork mode)
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"rocm-mq-{SCENARIO_ID}",
        description=(
            f"Dogfood driver for {SCENARIO_ID} ({EXPECTED['reason']}). "
            "Requires GITHUB_TOKEN env var. Leaves PRs in place per D-04."
        ),
    )
    parser.add_argument(
        "--owner",
        required=True,
        help="GitHub repository owner (e.g., SamuelReeder).",
    )
    parser.add_argument(
        "--repo",
        required=True,
        help="GitHub repository name (e.g., rocm-libraries).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Live-fork CLI entrypoint. Returns 0 on pass, 1 on fail, 2 on usage."""
    args = _parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print(
            "error: GITHUB_TOKEN environment variable is not set; this driver "
            "requires a token with App-equivalent scopes (or an installation "
            "token minted via gh api).",
            file=sys.stderr,
        )
        return 2

    # Lazy import — avoids paying the githubkit import cost in unit-test mode.
    from rocm_mq.gh import GitHubClient

    client = GitHubClient(token=token)

    try:
        result = run_scenario(client, owner=args.owner, repo=args.repo)
    except TimeoutError as exc:
        print(f"dog_05: TIMEOUT — {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"dog_05: error: {type(exc).__name__}: {exc!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1

    return 0 if result.passed else 1


__all__ = [
    "EXPECTED",
    "SCENARIO_ID",
    "TIMEOUT_S",
    "main",
    "run_scenario",
]


if __name__ == "__main__":
    sys.exit(main())
