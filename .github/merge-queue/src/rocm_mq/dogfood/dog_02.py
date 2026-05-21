"""
rocm_mq.dogfood.dog_02 — DOG-02 merge-conflict-at-activation driver.

RFC §6 scenario: A PR opted into a queue conflicts with the ``develop`` tip at
activation time → the processor MUST eject with the documented reason
``"merge conflict with develop"``.

Two-mode driver per CONTEXT.md D-04:

  1. Unit-test mode: ``run_scenario(client, ...)`` is called with a FakeGitHub
     extension that simulates the processor's eject status-comment. Verifies
     orchestration without touching the live API. Tests live in
     ``tests/test_dogfood_dog_02.py``.

  2. Live-fork mode: ``python -m rocm_mq.dogfood.dog_02 --owner SamuelReeder
     --repo rocm-libraries`` against the live fork. Operator-initiated AFTER
     ``mq-handler.yml`` + ``mq-processor.yml`` are deployed to ``develop`` on
     the fork and the App installation is live (plan 03-05). Drivers leave
     PRs in place per D-04 (no teardown). NOT exercised in CI.

Output: writes a per-run JSON to
``.planning/phases/03-handler-processor-on-fork/dogfood-runs/`` with the D-04
schema; ``passed=True`` iff observed_outcome matches ``EXPECTED`` exactly.

Timeout budget: 12 minutes (RESEARCH.md Area #10 — ≥3 cron cycles + CI start;
PRE-CONFIRM Task 1 option-a accepted defaults).
"""

from __future__ import annotations

import argparse
import base64
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
# Module constants — locked by 03-11 Task 2 behavior block.
# ---------------------------------------------------------------------------

SCENARIO_ID: str = "dog_02"
EXPECTED: dict[str, Any] = {"action": "Eject", "reason": "merge conflict with develop"}
TIMEOUT_S: int = 12 * 60  # 720s — RESEARCH.md Area #10

# The path the driver mutates on both develop (seed) AND the PR branch
# (variant). Both writes touch the SAME line, producing a deterministic
# merge conflict when the processor merges develop tip into the PR head at
# activation time (RFC §4.6 activation step).
_CONFLICT_FILE_PATH: str = "projects/hipdnn/dogfood-seed-conflict.txt"

# Documented eject reason substring the processor's status comment must
# contain (RFC §6, locked).
_EJECT_REASON_SUBSTRING: str = "merge conflict with develop"

# Marker the renderer embeds in every status comment body (comment.py).
_STATUS_MARKER: str = "<!-- rocm-mq-status -->"


# ---------------------------------------------------------------------------
# Predicate factory — polls for the eject status-comment body
# ---------------------------------------------------------------------------


def _build_eject_predicate(
    inject_eject_after: Callable[[int], None] | None = None,
) -> Callable[[Any, str, str, int], dict[str, Any] | None]:
    """Return a ``poll_pr_state`` predicate that fires when the eject lands.

    Predicate semantics: lists PR comments, finds any comment carrying the
    ``rocm-mq-status`` marker whose body also contains the documented eject
    reason substring; returns the observed-outcome dict on match, ``None``
    otherwise (keep polling).

    ``inject_eject_after`` is a unit-test seam: when supplied (non-None), it
    is invoked with the PR number on every poll attempt BEFORE the comment
    scan, so a FakeGitHub extension can simulate the processor posting the
    eject comment. In live-fork mode the caller passes ``None`` and the real
    processor posts the comment asynchronously.
    """

    def predicate(
        client: Any, owner: str, repo: str, pr_number: int
    ) -> dict[str, Any] | None:
        if inject_eject_after is not None:
            inject_eject_after(pr_number)
        resp = client.rest.issues.list_comments(owner, repo, pr_number)
        comments = list(resp.parsed_data or [])
        for c in comments:
            body = str(getattr(c, "body", ""))
            if _STATUS_MARKER in body and _EJECT_REASON_SUBSTRING in body:
                return {"action": "Eject", "reason": _EJECT_REASON_SUBSTRING}
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
    output_dir: Path | None = None,
    poll_interval_s: float = 15.0,
    poll_timeout_s: float | None = None,
    inject_eject_after: Callable[[int], None] | None = None,
) -> DogfoodResult:
    """Run the DOG-02 scenario end-to-end and emit the per-run JSON.

    Args:
        client: A ``GitHubClient`` (live) or a FakeGitHub extension (tests).
        owner / repo: Target repository (e.g., ``SamuelReeder/rocm-libraries``).
        output_dir: Where to write the per-run JSON. Defaults to the D-04
            location inside the planning tree (via ``emit_result``'s default).
        poll_interval_s: Seconds between polls; tests pass 0 to skip sleeps.
        poll_timeout_s: Polling budget override; ``None`` uses ``TIMEOUT_S``.
        inject_eject_after: Test-only seam — see ``_build_eject_predicate``.

    Returns:
        The ``DogfoodResult`` instance written to disk.

    Raises:
        TimeoutError: Polling budget elapsed before the eject comment landed.
    """
    timeout = TIMEOUT_S if poll_timeout_s is None else poll_timeout_s
    started = datetime.now(tz=UTC).isoformat()

    # 1. Seed develop with a conflict line BEFORE the PR branch is created.
    #    Writing directly to ``branch="develop"`` via the Contents API means
    #    the PR's branch (created next, off the new develop tip) ALREADY
    #    contains this seed. The PR's own file commit then mutates the same
    #    line — produces a deterministic merge conflict only at activation
    #    time, when the processor merges develop tip back into the PR head
    #    (RFC §4.6 activation step). The first-commit-into-develop pattern
    #    is documented as an accepted artifact per T-03-11-01 (threat model).
    seed_content_b64 = base64.b64encode(
        f"seed-variant-A: chosen at dogfood seed time {started}\n".encode("utf-8")
    ).decode("ascii")
    # On re-runs the seed file may already exist on develop; fetch its sha so
    # the contents API treats this as an update rather than a (failing) create.
    existing_sha: str | None = None
    try:
        resp = client.rest.repos.get_content(
            owner, repo, _CONFLICT_FILE_PATH, ref="develop"
        )
        data = resp.parsed_data
        existing_sha = str(getattr(data, "sha", "")) or None
    except Exception:
        existing_sha = None
    file_kwargs: dict[str, Any] = {
        "message": f"[dogfood {SCENARIO_ID}] seed develop with conflict bait",
        "content": seed_content_b64,
        "branch": "develop",
    }
    if existing_sha:
        file_kwargs["sha"] = existing_sha
    client.rest.repos.create_or_update_file_contents(
        owner,
        repo,
        _CONFLICT_FILE_PATH,
        **file_kwargs,
    )

    # 2. Create the PR (branched off the just-updated develop tip). The PR's
    #    file content writes a DIFFERENT variant to the same line — conflict
    #    deterministically materializes when develop is merged back in.
    pr_number, pr_url = create_dogfood_pr(
        client,
        owner,
        repo,
        SCENARIO_ID,
        _CONFLICT_FILE_PATH,
        "pr-variant-B: chosen at dogfood PR-open time\n",
        title_suffix="merge-conflict-at-activation",
    )

    # 3. Post the /merge command to enqueue the PR.
    comment_id = post_command(client, owner, repo, pr_number, "/merge")

    # 4. Poll for the eject status comment (timeout = TIMEOUT_S).
    predicate = _build_eject_predicate(inject_eject_after=inject_eject_after)
    observed = poll_pr_state(
        client,
        owner,
        repo,
        pr_number,
        predicate=predicate,
        timeout_s=timeout,
        interval_s=poll_interval_s,
    )
    ended = datetime.now(tz=UTC).isoformat()

    # 5. Build the DogfoodResult + emit per-run JSON.
    passed = observed == EXPECTED
    timeline: tuple[tuple[str, str, dict[str, Any]], ...] = (
        (started, "pr_opened", {"pr_number": pr_number, "pr_url": pr_url}),
        (
            started,
            "merge_command_posted",
            {"comment_id": comment_id, "command": "/merge"},
        ),
        (started, "mq_queued_label_applied", {"labels": ["mq:queued"]}),
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
    """Live-fork CLI entrypoint. Returns 0 on pass, 1 on fail, 2 on usage error."""
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
        print(f"dog_02: TIMEOUT — {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"dog_02: error: {type(exc).__name__}: {exc!r}", file=sys.stderr)
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
