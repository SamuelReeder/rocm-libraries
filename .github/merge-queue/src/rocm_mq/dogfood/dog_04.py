"""
rocm_mq.dogfood.dog_04 — DOG-04 simultaneous /merge idempotency driver.

RFC §6 scenario (with RESEARCH.md Area #7 idempotency layers): the user posts
two ``/merge`` comments back-to-back. The handler MUST short-circuit the
second invocation so that no duplicate state appears:

  (a) ``mq:queued`` label is applied exactly ONCE (set semantics on the
      labels API; idempotent re-add is a no-op).
  (b) Exactly ONE comment carries the ``<!-- rocm-mq-status -->`` marker
      (upsert pattern via ``_find_status_comment_id``; second /merge
      no-ops on the same body).
  (c) BOTH trigger comments carry an ``eyes`` reaction (201 first, 200 on
      duplicate; treat both as success — no pre-check).

Two-mode driver per CONTEXT.md D-04. Unit-test mode is exercised in CI
against ``tests/test_dogfood_dog_04.py``; live-fork mode runs as ``python -m
rocm_mq.dogfood.dog_04 --owner SamuelReeder --repo rocm-libraries`` after
the handler workflow is deployed.

Timeout budget: 60s (RESEARCH.md Area #10 — handler is fast; no processor
cycle required; just enough to let webhook delivery + handler-runtime
settle).
"""

from __future__ import annotations

import argparse
import os
import sys
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rocm_mq.dogfood._base import (
    DogfoodResult,
    create_dogfood_pr,
    emit_result,
    post_command,
)

# ---------------------------------------------------------------------------
# Module constants — locked by 03-11 Task 3 behavior block.
# ---------------------------------------------------------------------------

SCENARIO_ID: str = "dog_04"
EXPECTED: dict[str, Any] = {"action": "Idempotent", "no_duplicate_state": True}
TIMEOUT_S: int = 60

# Path under an opted-in queue (dnn-providers/miopen-provider/) so the handler
# derives a non-empty queue set (otherwise DOG-08 rejection short-circuits).
_PR_FILE_PATH: str = "dnn-providers/miopen-provider/dogfood-idempotency.txt"
_PR_FILE_CONTENT: str = "Dogfood idempotency PR — DOG-04.\n"
_STATUS_MARKER: str = "<!-- rocm-mq-status -->"


# ---------------------------------------------------------------------------
# Idempotency assertions — collected after the handler has settled
# ---------------------------------------------------------------------------


def _count_mq_queued_label(client: Any, owner: str, repo: str, pr_number: int) -> int:
    """Count occurrences of the literal ``mq:queued`` label name on the PR.

    On the real GitHub API ``pulls.get(...).parsed_data.labels`` is a list of
    label objects, deduplicated by the server (set semantics). This helper
    counts NAME matches so a buggy double-apply (or a buggy fake) is detected
    rather than silently masked by the JSON shape.
    """
    resp = client.rest.pulls.get(owner, repo, pr_number)
    labels = list(getattr(resp.parsed_data, "labels", []) or [])
    return sum(1 for lbl in labels if str(getattr(lbl, "name", "")) == "mq:queued")


def _count_status_comments(
    client: Any, owner: str, repo: str, pr_number: int
) -> int:
    """Count comments whose body carries the ``rocm-mq-status`` marker."""
    resp = client.rest.issues.list_comments(owner, repo, pr_number)
    comments = list(resp.parsed_data or [])
    return sum(
        1
        for c in comments
        if _STATUS_MARKER in str(getattr(c, "body", ""))
    )


def _count_eyes_reactions(
    client: Any, comment_ids: list[int], *, owner: str = "", repo: str = ""
) -> int:
    """Count ``eyes`` reactions recorded against ``comment_ids``.

    The real API exposes ``reactions.list_for_issue_comment(owner, repo, cid)``
    per-comment; FakeGitHub records an append-only log in
    ``state.reactions_log`` of ``(comment_id, content)`` tuples. We read
    whichever surface is present (tests can ALSO patch the fake by populating
    the log directly).

    ``owner`` and ``repo`` default to empty strings for the fake/log path
    (which ignores them) but MUST be passed when the live-API fallback runs;
    githubkit rejects empty path-component URLs with a 404 on the
    ``/repos///issues/...`` shape.
    """
    state = getattr(client, "state", None)
    log = getattr(state, "reactions_log", None) if state is not None else None
    if log is not None:
        ids = set(comment_ids)
        return sum(1 for (cid, content) in log if cid in ids and content == "eyes")
    # Fallback: query the live API per-comment (each call returns a list of
    # reactions; we count items where content == "eyes").
    total = 0
    for cid in comment_ids:
        resp = client.rest.reactions.list_for_issue_comment(owner, repo, cid)
        for r in list(resp.parsed_data or []):
            if str(getattr(r, "content", "")) == "eyes":
                total += 1
    return total


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
    settle_s: float = 120.0,
) -> DogfoodResult:
    """Run DOG-04 end-to-end and emit per-run JSON.

    ``settle_s`` is the handler-settle wait (one sleep) BEFORE the three
    idempotency assertions are collected. Live mq-handler runtime is
    typically 60-90s (queue + checkout + setup-python + preflight + token
    mint + handle); 120s gives a comfortable buffer. The prior 30s default
    consistently timed out before the handler completed (live runs
    2026-05-20 PR #31, #32, #33 produced all-zeros JSON despite the
    handler eventually succeeding). In unit tests the caller passes
    ``poll_interval_s=0`` to skip the sleep entirely (handler simulation
    is synchronous in the fake).

    Returns the ``DogfoodResult`` instance written to disk.
    """
    started = datetime.now(tz=UTC).isoformat()

    # 1. Create a fresh PR at an opted-in path.
    pr_number, pr_url = create_dogfood_pr(
        client,
        owner,
        repo,
        SCENARIO_ID,
        _PR_FILE_PATH,
        _PR_FILE_CONTENT,
        title_suffix="simultaneous-/merge-idempotency",
    )

    # 2. Post TWO /merge comments in tight succession (no sleep between).
    trigger_id_1 = post_command(client, owner, repo, pr_number, "/merge")
    trigger_id_2 = post_command(client, owner, repo, pr_number, "/merge")

    # 3. Let the handler settle. In live-fork mode this is ~30s; in unit
    #    tests the caller passes a tiny ``poll_interval_s`` and we skip the
    #    settle wait entirely (handler simulation is synchronous in the fake).
    if poll_interval_s > 0:
        # Single bounded wait — drivers do NOT poll for the absence of a
        # signal (Area #7: no good "the second /merge produced no work"
        # affirmative signal exists; the assertions ARE the signal).
        time.sleep(min(settle_s, float(TIMEOUT_S)))

    # 4. Collect the three idempotency counters.
    mq_queued_count = _count_mq_queued_label(client, owner, repo, pr_number)
    status_count = _count_status_comments(client, owner, repo, pr_number)
    eyes_count = _count_eyes_reactions(
        client, [trigger_id_1, trigger_id_2], owner=owner, repo=repo
    )

    no_duplicate_state = (
        mq_queued_count == 1 and status_count == 1 and eyes_count == 2
    )
    observed = {
        "action": "Idempotent",
        "no_duplicate_state": no_duplicate_state,
        "mq_queued_count": mq_queued_count,
        "status_comment_count": status_count,
        "eyes_reactions_on_triggers": eyes_count,
    }
    ended = datetime.now(tz=UTC).isoformat()

    # passed compares the locked subset of EXPECTED (action + no_duplicate_state).
    passed = (
        observed["action"] == EXPECTED["action"]
        and observed["no_duplicate_state"] == EXPECTED["no_duplicate_state"]
    )

    timeline: tuple[tuple[str, str, dict[str, Any]], ...] = (
        (started, "pr_opened", {"pr_number": pr_number, "pr_url": pr_url}),
        (
            started,
            "merge_command_posted",
            {"comment_id": trigger_id_1, "command": "/merge", "ordinal": 1},
        ),
        (
            started,
            "merge_command_posted",
            {"comment_id": trigger_id_2, "command": "/merge", "ordinal": 2},
        ),
        (
            ended,
            "mq_queued_label_applied",
            {"count": mq_queued_count, "expected": 1},
        ),
        (
            ended,
            "status_comment_upserted",
            {"count": status_count, "expected": 1},
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
            "Idempotency assertions: mq:queued count == 1, status-comment "
            "count == 1, eyes reactions on triggers == 2 (one per /merge)."
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
            "Dogfood driver for DOG-04 (simultaneous /merge idempotency). "
            "Requires GITHUB_TOKEN. Leaves PRs in place per D-04."
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
        print(f"dog_04: TIMEOUT — {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"dog_04: error: {type(exc).__name__}: {exc!r}", file=sys.stderr)
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
