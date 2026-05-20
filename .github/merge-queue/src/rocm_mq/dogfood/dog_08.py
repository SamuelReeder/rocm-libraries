"""
rocm_mq.dogfood.dog_08 — DOG-08 handler-level rejection driver.

RFC §6 / plan 03-06 scenario: a PR whose changed files do NOT match any
opted-in path in ``path_to_queues.yml`` (and are NOT under the ``dogfood/``
canary tree, which routes to ``dogfood-canary``) gets rejected by the handler
BEFORE any state mutation:

  (a) NO ``mq:*`` labels applied,
  (b) ≥1 bot comment whose body contains the rejection-reason substring
      ``no opted-in`` (from ``cmd_handle.py``'s literal text),
  (c) NO comment carries the ``<!-- rocm-mq-status -->`` marker.

The target PR path ``DOCS/dogfood-no-opted-in.md`` is verified absent from
``path_to_queues.yml`` (a unit test asserts this). The path is upper-case
``DOCS/`` to distinguish from any future ``docs/`` opt-in.

Two-mode driver per CONTEXT.md D-04. Unit-test mode is exercised in CI;
live-fork mode runs as ``python -m rocm_mq.dogfood.dog_08 --owner SamuelReeder
--repo rocm-libraries`` after the handler workflow is deployed.

Timeout budget: 60s (RESEARCH.md Area #10 — handler rejection is fast).
"""

from __future__ import annotations

import argparse
import os
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
# Module constants — locked by 03-11 Task 4 behavior block.
# ---------------------------------------------------------------------------

SCENARIO_ID: str = "dog_08"
EXPECTED: dict[str, Any] = {
    "action": "Reject",
    "level": "handler",
    "reason_substring": "no opted-in path",
}
TIMEOUT_S: int = 60

# Path NOT under any opted-in queue per .github/merge-queue/path_to_queues.yml
# (verified by tests/test_dogfood_dog_08.py). Upper-case DOCS/ avoids a
# collision with any future docs/ opt-in. Cannot be under dogfood/ (that
# routes to dogfood-canary per the YAML's synthetic queue entry).
PR_FILE_PATH: str = "DOCS/dogfood-no-opted-in.md"
_PR_FILE_CONTENT: str = (
    "# DOG-08 — dogfood marker file\n"
    "\n"
    "This PR exists to exercise the merge-queue handler's `no opted-in path` "
    "rejection branch (RFC §6, plan 03-06 cmd_handle.py). Driver leaves the "
    "PR in place per CONTEXT.md D-04.\n"
)

# Marker the renderer embeds in every status comment body.
_STATUS_MARKER: str = "<!-- rocm-mq-status -->"

# Substring the handler's rejection comment body MUST contain (cmd_handle.py
# `_handle_merge` empty-queue-set branch). The literal text is anchored on
# the phrase ``no opted-in queue paths``; we match the shorter
# ``no opted-in`` to stay robust to minor wording drift.
_REJECTION_SUBSTRING: str = "no opted-in"


# ---------------------------------------------------------------------------
# Predicate factory — polls for the rejection comment
# ---------------------------------------------------------------------------


def _build_rejection_predicate(
    inject_after: Callable[[int], None] | None = None,
) -> Callable[[Any, str, str, int], dict[str, Any] | None]:
    """Return a predicate that fires when a rejection comment lands.

    Returns the observed-outcome dict on detection; ``None`` otherwise. The
    optional ``inject_after`` seam mirrors the dog_02 pattern for symmetry.
    """

    def predicate(
        client: Any, owner: str, repo: str, pr_number: int
    ) -> dict[str, Any] | None:
        if inject_after is not None:
            inject_after(pr_number)
        resp = client.rest.issues.list_comments(owner, repo, pr_number)
        comments = list(resp.parsed_data or [])
        for c in comments:
            body = str(getattr(c, "body", ""))
            # Skip the status comment if any leaked through (assertion below
            # checks for absence; we only want to fire on the rejection body).
            if _STATUS_MARKER in body:
                continue
            if _REJECTION_SUBSTRING in body:
                return {
                    "action": "Reject",
                    "level": "handler",
                    "reason_substring": _REJECTION_SUBSTRING + " path",
                    "rejection_comment_body": body,
                }
        return None

    return predicate


# ---------------------------------------------------------------------------
# Idempotency / state assertions — collected post-rejection
# ---------------------------------------------------------------------------


def _count_mq_labels(client: Any, owner: str, repo: str, pr_number: int) -> int:
    resp = client.rest.pulls.get(owner, repo, pr_number)
    labels = list(getattr(resp.parsed_data, "labels", []) or [])
    return sum(
        1
        for lbl in labels
        if str(getattr(lbl, "name", "")).startswith("mq:")
    )


def _count_status_comments(
    client: Any, owner: str, repo: str, pr_number: int
) -> int:
    resp = client.rest.issues.list_comments(owner, repo, pr_number)
    comments = list(resp.parsed_data or [])
    return sum(
        1
        for c in comments
        if _STATUS_MARKER in str(getattr(c, "body", ""))
    )


# ---------------------------------------------------------------------------
# run_scenario — orchestration body
# ---------------------------------------------------------------------------


def run_scenario(
    client: Any,
    *,
    owner: str,
    repo: str,
    output_dir: Path | None = None,
    poll_interval_s: float = 5.0,
    poll_timeout_s: float | None = None,
) -> DogfoodResult:
    """Run DOG-08 end-to-end and emit per-run JSON."""
    timeout = TIMEOUT_S if poll_timeout_s is None else poll_timeout_s
    started = datetime.now(tz=UTC).isoformat()

    # 1. Create a fresh PR at a NON-opted-in path.
    pr_number, pr_url = create_dogfood_pr(
        client,
        owner,
        repo,
        SCENARIO_ID,
        PR_FILE_PATH,
        _PR_FILE_CONTENT,
        title_suffix="handler-rejection-no-opted-in-path",
    )

    # 2. Post /merge and poll for the rejection comment.
    comment_id = post_command(client, owner, repo, pr_number, "/merge")
    predicate = _build_rejection_predicate()
    observed_predicate = poll_pr_state(
        client,
        owner,
        repo,
        pr_number,
        predicate=predicate,
        timeout_s=timeout,
        interval_s=poll_interval_s,
    )

    # 3. Collect the three rejection-state assertions.
    mq_label_count = _count_mq_labels(client, owner, repo, pr_number)
    status_count = _count_status_comments(client, owner, repo, pr_number)

    observed = {
        "action": "Reject",
        "level": "handler",
        "reason_substring": observed_predicate["reason_substring"],
        "mq_label_count": mq_label_count,
        "status_comment_count": status_count,
        "rejection_comment_found": True,
    }

    # passed iff all three assertions hold.
    passed = (
        mq_label_count == 0
        and status_count == 0
        and observed_predicate is not None
    )

    ended = datetime.now(tz=UTC).isoformat()
    timeline: tuple[tuple[str, str, dict[str, Any]], ...] = (
        (started, "pr_opened", {"pr_number": pr_number, "pr_url": pr_url}),
        (
            started,
            "merge_command_posted",
            {"comment_id": comment_id, "command": "/merge"},
        ),
        (
            ended,
            "handler_rejection_comment_posted",
            {"reason_substring": _REJECTION_SUBSTRING + " path"},
        ),
        (
            ended,
            "no_mq_labels_present",
            {"count": mq_label_count, "expected": 0},
        ),
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
            "Handler-level rejection — no processor cycle expected. "
            "Assertions: zero mq:* labels, zero <!-- rocm-mq-status --> "
            "comments, ≥1 bot comment containing 'no opted-in'."
        ),
    )
    emit_result(result, output_dir=output_dir)
    return result


# ---------------------------------------------------------------------------
# main() — CLI entrypoint
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=f"rocm-mq-{SCENARIO_ID}",
        description=(
            "Dogfood driver for DOG-08 (handler-level rejection on no "
            "opted-in path). Requires GITHUB_TOKEN. Leaves PRs in place "
            "per D-04."
        ),
    )
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print(
            "error: GITHUB_TOKEN environment variable is not set.",
            file=sys.stderr,
        )
        return 2

    from rocm_mq.gh import GitHubClient

    client = GitHubClient(token=token)

    try:
        result = run_scenario(client, owner=args.owner, repo=args.repo)
    except TimeoutError as exc:
        print(f"dog_08: TIMEOUT — {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"dog_08: error: {type(exc).__name__}: {exc!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1

    return 0 if result.passed else 1


__all__ = [
    "EXPECTED",
    "PR_FILE_PATH",
    "SCENARIO_ID",
    "TIMEOUT_S",
    "main",
    "run_scenario",
]


if __name__ == "__main__":
    sys.exit(main())
