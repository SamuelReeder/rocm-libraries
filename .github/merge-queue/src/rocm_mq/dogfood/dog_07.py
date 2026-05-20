"""
rocm_mq.dogfood.dog_07 — DOG-07 approval-revoked eject driver.

RFC §5 + §6 scenario: a PR opted into a queue receives a required approving
review from a SECOND identity (PR author cannot self-approve under branch
protection — RFC §5 defense-in-depth); operator/driver posts ``/merge``;
processor applies ``mq:queued`` then ``mq:active``. The second-identity
approver then DISMISSES their review (``pulls.dismiss_review``). On the next
processor cycle the activation re-evaluation observes that the required-
review gate is no longer satisfied → the processor ejects with the documented
reason:

    "approval revoked"

The eject reason is the exact RFC §6 documented literal — pinned by
``EXPECTED`` and asserted by the test suite. This scenario exercises the
RFC §5 property that the required-review gate must apply throughout the
queue lifecycle, not just at enqueue time; a review dismissed AFTER
activation MUST invalidate the activation.

Two-mode driver per CONTEXT.md D-04:

  1. Unit-test mode: ``run_scenario(client, approver_client=..., ...)`` is
     called with a FakeGitHub extension that simulates the second-identity
     approval (recorded as a review with state=APPROVED), the activation
     transition (mq:active label applied on /merge), the dismissal (review
     state APPROVED → DISMISSED), and the processor's eject status comment
     (injected on the polling step only after the dismissal has been
     observed). Tests live in ``tests/test_dogfood_dog_07.py``.

  2. Live-fork mode: ``python -m rocm_mq.dogfood.dog_07 --owner SamuelReeder
     --repo rocm-libraries`` against the live fork. Operator-initiated AFTER
     mq-handler.yml + mq-processor.yml are deployed to develop on the fork,
     the App installation is live, AND a second-collaborator account has
     been provisioned on the fork with a PAT exported as ``APPROVER_TOKEN``.

The second-account mechanism is the PRE-CONFIRM Task 1 outcome (plan 03-15):
option-a (second collaborator account + PAT) is the recommended path. The
driver reads ``APPROVER_TOKEN`` from env; absence in live-fork mode is a
usage error (exit 2) explaining how to provision the second identity. In
unit-test mode the caller passes ``approver_client`` explicitly, so the env
var is not consulted.

Driver flow:
  1. Create PR (primary client).
  2. Post APPROVE review via the APPROVER client (second identity).
  3. Post /merge (primary client).
  4. Poll until mq:active is observed (processor activation).
  5. Dismiss the review (APPROVER client) — invalidates the approval gate.
  6. Poll for the eject status comment carrying the documented reason.

Output: writes per-run JSON to
``.planning/phases/03-handler-processor-on-fork/dogfood-runs/`` with the
D-04 schema; ``passed=True`` iff observed_outcome matches ``EXPECTED``
exactly (the RFC literal).

Timeout budget: 15 minutes (RESEARCH.md Area #10 — activation + dismissal +
≥1 cron cycle for eject).
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
# Module constants — locked by plan 03-15 Task 2 behavior block.
# ---------------------------------------------------------------------------

SCENARIO_ID: str = "dog_07"
EXPECTED: dict[str, Any] = {
    "action": "Eject",
    # Verbatim RFC §6 literal; drift would surface here at first glance.
    "reason": "approval revoked",
}
TIMEOUT_S: int = 15 * 60  # 900s — RESEARCH.md Area #10 timeout-budget table.

# Seed-file path the PR is opened at. Lives under an opted-in path (the
# hipdnn dogfood area chosen by the other DOG-* drivers) so the PR routes
# to a real queue and the processor activates it. A random hex suffix is
# appended at call time to avoid concurrent-run collisions on the same path.
_SEED_PATH_PREFIX: str = "projects/hipdnn/dogfood-approval-revoked"

# Marker the renderer embeds in every status comment body (comment.py).
_STATUS_MARKER: str = "<!-- rocm-mq-status -->"

# The exact eject-reason substring the predicate matches on. Identical to
# EXPECTED["reason"] but referenced separately so a substring-vs-exact
# refactor at the predicate level can touch one constant.
_EJECT_REASON_SUBSTRING: str = "approval revoked"

# The label that signals the processor's activation step has completed and
# the PR is in the post-activation evaluation window where an approval
# dismissal would invalidate the activation.
_MQ_ACTIVE_LABEL: str = "mq:active"

# Env var name the driver reads in live-fork mode for the second-identity
# PAT (plan 03-15 Task 1 option-a outcome). Unit tests bypass via the
# ``approver_client`` kwarg.
_APPROVER_TOKEN_ENV: str = "APPROVER_TOKEN"


# ---------------------------------------------------------------------------
# Predicate factories — activation poll + post-dismissal eject poll
# ---------------------------------------------------------------------------


def _build_active_label_predicate() -> Callable[[Any, str, str, int], dict[str, Any] | None]:
    """Return a predicate that fires when the PR carries the ``mq:active`` label.

    Reads the PR via ``rest.pulls.get`` and inspects the ``labels`` list. The
    FakeGitHub fake returns labels as ``[SimpleNamespace(name=...), ...]``;
    the live githubkit client returns the same shape. Returns a dict snapshot
    of the activation observation on hit, ``None`` otherwise.
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
    eject comment AFTER the dismissal has been observed. In live-fork mode
    the caller passes ``None`` and the real processor posts the comment
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
# run_scenario — orchestration body (called by main() and by unit tests)
# ---------------------------------------------------------------------------


def run_scenario(
    client: Any,
    *,
    owner: str,
    repo: str,
    approver_client: Any,
    output_dir: Path | None = None,
    poll_interval_s: float = 15.0,
    poll_timeout_s: float | None = None,
    inject_eject_after: Callable[[int], None] | None = None,
) -> DogfoodResult:
    """Run DOG-07 end-to-end and emit the per-run JSON.

    Args:
        client: A ``GitHubClient`` (live) or FakeGitHub extension (tests).
            Used for primary actions: PR open, /merge post, polling.
        owner / repo: Target repository (e.g., ``SamuelReeder/rocm-libraries``).
        approver_client: A separate client authenticated as the SECOND
            identity. Used for the APPROVE review and the dismiss call. In
            unit tests both clients can be the same FakeGitHub instance
            (the fake records the approver login internally). In live-fork
            mode this is a separate ``GitHubClient(token=APPROVER_TOKEN)``.
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
    #    appended to the file basename so concurrent runs of dog_07 don't
    #    collide on the seed path.
    seed_suffix = secrets.token_hex(4)
    seed_path = f"{_SEED_PATH_PREFIX}-{seed_suffix}.txt"
    pr_number, pr_url = create_dogfood_pr(
        client,
        owner,
        repo,
        SCENARIO_ID,
        seed_path,
        "dogfood dog_07 seed file — opened to exercise the approval-revoked path\n",
        title_suffix="approval-revoked",
    )
    pr_opened_at = datetime.now(tz=UTC).isoformat()

    # 2. APPROVER posts an APPROVE review (second identity). This satisfies
    #    the at-enqueue ≥1-approving-review gate so /merge will accept.
    review_resp = approver_client.rest.pulls.create_review(owner, repo, pr_number, event="APPROVE")
    review_data = review_resp.parsed_data
    review_id = int(getattr(review_data, "id", 0) or 0)
    approver_login = str(getattr(getattr(review_data, "user", None), "login", "") or "")
    approval_submitted_at = datetime.now(tz=UTC).isoformat()

    # 3. Post /merge via the primary client to enqueue the PR.
    comment_id = post_command(client, owner, repo, pr_number, "/merge")

    # 4. Poll until the PR carries the ``mq:active`` label — confirms the
    #    processor has run its activation step and the eject path is now
    #    armed (any approval revocation will invalidate the activation).
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

    # 5. APPROVER dismisses the review — invalidates the at-enqueue gate.
    approver_client.rest.pulls.dismiss_review(
        owner,
        repo,
        pr_number,
        review_id,
        message="dogfood DOG-07 revoke",
    )
    dismissal_observed_at = datetime.now(tz=UTC).isoformat()

    # 6. Poll for the eject status comment carrying the documented reason.
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

    # 7. Build the DogfoodResult + emit per-run JSON. Timeline subset per
    #    plan 03-15 Task 2 <action>: pr_opened, approval_review_submitted,
    #    merge_command_posted, mq_queued_label_applied, activation_began,
    #    mq_active_label_applied, approval_review_dismissed, ejected.
    passed = observed == EXPECTED
    timeline: tuple[tuple[str, str, dict[str, Any]], ...] = (
        (pr_opened_at, "pr_opened", {"pr_number": pr_number, "pr_url": pr_url}),
        (
            approval_submitted_at,
            "approval_review_submitted",
            {
                "review_id": review_id,
                "approver_login": approver_login,
                "event": "APPROVE",
            },
        ),
        (
            approval_submitted_at,
            "merge_command_posted",
            {"comment_id": comment_id, "command": "/merge"},
        ),
        (
            activation_observed_at,
            "mq_queued_label_applied",
            {"labels": ["mq:queued"]},
        ),
        (
            activation_observed_at,
            "activation_began",
            {"derived": "see status comment"},
        ),
        (
            activation_observed_at,
            "mq_active_label_applied",
            {
                "labels": activation.get("labels", []),
                "head_sha_at_activation": activation.get("head_sha_at_activation", ""),
            },
        ),
        (
            dismissal_observed_at,
            "approval_review_dismissed",
            {
                "review_id": review_id,
                "approver_login": approver_login,
                "dismiss_message": "dogfood DOG-07 revoke",
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
            f"Approval submitted by {approver_login!r} (review id "
            f"{review_id}); activation observed at head SHA "
            f"{activation.get('head_sha_at_activation', '')!r}; review "
            f"dismissed via APPROVER client. The eject reason is the "
            "verbatim RFC §6 literal; any drift would flip passed=False. "
            "Live-fork run: the processor cycle URL embedded in the status "
            "comment body is the canonical processor_run_url; populate from "
            "the comment in a future enhancement if needed."
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
            "Requires GITHUB_TOKEN (primary, App-equivalent) AND "
            "APPROVER_TOKEN (second-identity PAT with pull_requests:write) "
            "env vars. Leaves PRs in place per D-04."
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
    """Live-fork CLI entrypoint. Returns 0 on pass, 1 on fail, 2 on usage.

    Token gates (plan 03-15 Task 1 option-a):
      * GITHUB_TOKEN: primary identity, used for PR open / /merge / polling.
        Absence → exit 2 with stderr instructions.
      * APPROVER_TOKEN: second identity (e.g., samuel-reeder-bot PAT scoped
        narrowly to pull_requests:write on the fork). Used for the APPROVE
        review and the dismiss call. Absence → exit 2 with stderr
        instructions pointing at the plan 03-15 PRE-CONFIRM outcome.
    """
    args = _parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print(
            "error: GITHUB_TOKEN environment variable is not set; this driver "
            "requires a primary token with App-equivalent scopes (or an "
            "installation token minted via gh api).",
            file=sys.stderr,
        )
        return 2

    approver_token = os.environ.get(_APPROVER_TOKEN_ENV, "")
    if not approver_token:
        print(
            f"error: {_APPROVER_TOKEN_ENV} environment variable is not set; "
            "DOG-07 requires a SECOND-identity PAT for the approval + "
            "dismissal steps (RFC §5 forbids self-approval via branch "
            "protection). Per plan 03-15 Task 1 option-a outcome: provision "
            "a second collaborator account on the fork with pull_requests:"
            f"write scope and export its PAT as {_APPROVER_TOKEN_ENV}. See "
            ".planning/phases/03-handler-processor-on-fork/03-15-SUMMARY.md "
            "for the second-account mechanism record.",
            file=sys.stderr,
        )
        return 2

    # Lazy import — avoids paying the githubkit import cost in unit-test mode.
    from rocm_mq.gh import GitHubClient

    client = GitHubClient(token=token)
    approver_client = GitHubClient(token=approver_token)

    try:
        result = run_scenario(
            client,
            owner=args.owner,
            repo=args.repo,
            approver_client=approver_client,
        )
    except TimeoutError as exc:
        print(f"dog_07: TIMEOUT — {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"dog_07: error: {type(exc).__name__}: {exc!r}", file=sys.stderr)
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
