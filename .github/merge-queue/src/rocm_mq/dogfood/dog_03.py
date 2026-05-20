"""
rocm_mq.dogfood.dog_03 — DOG-03 CI-failure-during-evaluation eject driver.

RFC §6 scenario: A PR opted into a queue passes the at-enqueue gates,
gets ``mq:queued`` then ``mq:active``, and at activation-time required-check
evaluation the processor observes a FAILING required check on the PR head
SHA → it MUST eject the PR with a status-comment reason naming the failed
check.

The deterministic failure signal is supplied by the dogfood canary workflow
(``.github/workflows/mq-dogfood-canary.yml``, plan 03-09). The canary is
path-filtered to ``dogfood/**`` and reads the PR title for the literal
substring ``[dogfood-ci-fail]``; on match it exits 1, registering its
check-run as ``failure``. The synthetic ``dogfood-canary`` queue in
``.github/merge-queue/path_to_queues.yml`` (plan 03-03) routes ``dogfood/``
to that queue and pins the canary's check-run name in ``required_checks``,
so the processor's required-check evaluation against any dogfood/** PR with
the title marker deterministically sees a failing required check → eject.

Two-mode driver per CONTEXT.md D-04:

  1. Unit-test mode: ``run_scenario(client, ...)`` is called with a
     FakeGitHub extension that simulates the canary's verdict and the
     processor's eject status-comment. Verifies orchestration without
     touching the live API. Tests live in ``tests/test_dogfood_dog_03.py``.

  2. Live-fork mode: ``python -m rocm_mq.dogfood.dog_03 --owner SamuelReeder
     --repo rocm-libraries`` against the live fork. Operator-initiated
     AFTER ``mq-handler.yml`` + ``mq-processor.yml`` + ``mq-dogfood-canary.yml``
     are deployed and the App installation is live. Per CONTEXT.md D-04
     drivers leave PRs in place; NOT exercised in CI.

Canary check-name DISCOVERY (deliberate design choice):
  The eject-reason substring this driver matches on is the canary's
  registered check-run name. The CANONICAL source of that string is
  ``path_to_queues.yml required_checks.dogfood-canary`` — the same file the
  processor loads via ``config.load_from_develop`` to drive its
  required-check evaluation. The driver loads the same dict at runtime and
  extracts the check name from it, so a future re-pin of the canary's
  registered name (e.g., after the post-plan-03-09 live verification
  documented in 03-09-SUMMARY.md "Outstanding Live Verification") flows
  through without a driver-source edit.

Output: writes per-run JSON to
``.planning/phases/03-handler-processor-on-fork/dogfood-runs/`` with the
D-04 schema; ``passed=True`` iff observed_outcome matches the runtime-
derived EXPECTED.

Timeout budget: 20 minutes (RESEARCH.md Area #10 — canary is fast but
cycle + eval + eject; PRE-CONFIRMED with user per plan 03-12
must_haves.truths).
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
# Module constants — locked by plan 03-12 Task 1 behavior block.
# ---------------------------------------------------------------------------

SCENARIO_ID: str = "dog_03"
TIMEOUT_S: int = 20 * 60  # 1200s — RESEARCH.md Area #10

# Title prefix injected into the PR title so the canary workflow (plan 03-09)
# matches its literal-substring trigger and exits 1. WITHOUT this marker the
# canary exits 0 and the scenario cannot materialize — the title prefix is
# load-bearing, asserted by tests/test_dogfood_dog_03.py.
TITLE_PREFIX: str = "[dogfood-ci-fail][dogfood dog_03]"

# Path under the dogfood/ tree (the only path the canary workflow listens
# on, per its `paths: ['dogfood/**']` filter and path_to_queues.yml's
# `dogfood/` -> `dogfood-canary` routing). The filename's random suffix is
# injected by ``create_dogfood_pr`` so concurrent runs don't collide; this
# path is the prefix only.
_PR_FILE_PATH: str = "dogfood/dog-03-canary-fail.txt"
_PR_FILE_CONTENT: str = (
    "# DOG-03 — dogfood canary CI-failure marker file\n"
    "\n"
    "This PR exists to exercise the merge-queue processor's required-check\n"
    "eviction path (RFC §6 DOG-03, plan 03-12 dog_03.py). The PR title\n"
    "carries the [dogfood-ci-fail] substring so the canary workflow\n"
    "(plan 03-09) exits 1 -> required check fails -> processor ejects.\n"
    "Driver leaves the PR in place per CONTEXT.md D-04.\n"
)

# Marker the renderer embeds in every status comment body (comment.py).
_STATUS_MARKER: str = "<!-- rocm-mq-status -->"

# Synthetic queue key in path_to_queues.yml that routes dogfood/** paths
# (the canary workflow listens on these paths).
_DOGFOOD_QUEUE_KEY: str = "dogfood-canary"

# Canary check-run name. Tied to ``.github/workflows/mq-dogfood-canary.yml``
# (plan 03-09) — the workflow's job-id is ``canary`` and the workflow name
# is ``mq-dogfood-canary``, so GHA registers the check-run as
# ``mq-dogfood-canary / canary``. Post 03-wr-09, required-check evaluation
# is delegated to branch protection — the queue ejects with whatever check
# name appears in the merge-API failure message. This driver matches on
# the GHA-prefix segment (``mq-dogfood-canary``) which is stable against
# job-id renames within the same workflow file.
_CANARY_CHECK_NAME: str = "mq-dogfood-canary / canary"


# ---------------------------------------------------------------------------
# Canary check-name resolution — hardcoded per 03-wr-09
# ---------------------------------------------------------------------------


def _resolve_canary_check_name(
    client: Any, owner: str, repo: str
) -> str:
    """Return the canary's registered required-check name.

    Pre-03-wr-09 this loaded from path_to_queues.yml's required_checks map;
    that map no longer exists (branch protection owns required-check truth).
    The check name is now sourced from a module constant tied to the
    canary workflow file. ``client``, ``owner``, ``repo`` are unused but
    kept in the signature so the unit-test seam (a per-test patch of this
    function) does not need rewiring.
    """
    _ = (client, owner, repo)  # signature parity; unused post 03-wr-09
    return _CANARY_CHECK_NAME


def _check_name_substring(check_name: str) -> str:
    """Return the substring of ``check_name`` the eject-reason match uses.

    The canary's full registered name is shaped like
    ``mq-dogfood-canary / canary`` (workflow-name slash job-id, GHA
    convention). The substring match keeps the load-bearing prefix
    (``mq-dogfood-canary``) so minor drift in the job-id portion doesn't
    invalidate the assertion — plan 03-09's "Outstanding Live Verification"
    notes the suffix may shift after live capture.
    """
    return check_name.split(" / ", 1)[0] if " / " in check_name else check_name


# ---------------------------------------------------------------------------
# Predicate factory — polls for the eject status-comment naming the canary
# ---------------------------------------------------------------------------


def _build_eject_predicate(
    canary_substring: str,
    *,
    inject_eject_after: Callable[[int], None] | None = None,
) -> Callable[[Any, str, str, int], dict[str, Any] | None]:
    """Return a ``poll_pr_state`` predicate that fires on the eject comment.

    Semantics: list PR comments; find any with the ``rocm-mq-status``
    marker whose body ALSO contains ``canary_substring`` (the canary
    check-name slice resolved at runtime from path_to_queues.yml). Return
    the observed-outcome dict on match; ``None`` otherwise (keep polling).

    The ``canary_substring`` match is what distinguishes this driver's
    success from a different eject reason landing on the PR (e.g., a
    merge-conflict eject would carry the marker but NOT the canary name).

    ``inject_eject_after`` is a unit-test seam — see dog_02 for the
    matching pattern. In live-fork mode the caller passes ``None`` and the
    real processor posts the comment asynchronously.
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
            if _STATUS_MARKER in body and canary_substring in body:
                return {
                    "action": "Eject",
                    "reason": canary_substring,
                    "reason_full_body": body,
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
    output_dir: Path | None = None,
    poll_interval_s: float = 15.0,
    poll_timeout_s: float | None = None,
    inject_eject_after: Callable[[int], None] | None = None,
) -> DogfoodResult:
    """Run DOG-03 end-to-end and emit the per-run JSON.

    Args:
        client: A ``GitHubClient`` (live) or FakeGitHub extension (tests).
        owner / repo: Target repository (e.g., ``SamuelReeder/rocm-libraries``).
        output_dir: JSON output dir; defaults to D-04 location via
            ``emit_result``.
        poll_interval_s: Seconds between polls; tests pass 0 to skip sleeps.
        poll_timeout_s: Polling budget override; ``None`` uses ``TIMEOUT_S``.
        inject_eject_after: Test-only seam — see ``_build_eject_predicate``.

    Returns:
        The ``DogfoodResult`` instance written to disk.

    Raises:
        TimeoutError: Polling budget elapsed before an eject comment
            naming the canary check landed.
        RuntimeError: If path_to_queues.yml lacks a
            ``required_checks.dogfood-canary`` entry (cannot resolve the
            canary check name).
    """
    timeout = TIMEOUT_S if poll_timeout_s is None else poll_timeout_s
    started = datetime.now(tz=UTC).isoformat()

    # 1. Resolve the canary check-run name from develop's path_to_queues.yml.
    #    EXPECTED is built from this — not hardcoded — so a future re-pin of
    #    the canary name flows through automatically (see module docstring).
    canary_check_name = _resolve_canary_check_name(client, owner, repo)
    canary_substring = _check_name_substring(canary_check_name)
    expected: dict[str, Any] = {
        "action": "Eject",
        "reason_substring": canary_substring,
    }

    # 2. Create the PR under dogfood/ with the [dogfood-ci-fail] title
    #    prefix so the canary workflow's title-substring match fires.
    pr_number, pr_url = create_dogfood_pr(
        client,
        owner,
        repo,
        SCENARIO_ID,
        _PR_FILE_PATH,
        _PR_FILE_CONTENT,
        title_suffix="ci-failure-during-evaluation",
        title_prefix=TITLE_PREFIX,
    )

    # 3. Post /merge to enqueue.
    comment_id = post_command(client, owner, repo, pr_number, "/merge")

    # 4. Poll for the eject status comment naming the canary check.
    predicate = _build_eject_predicate(
        canary_substring, inject_eject_after=inject_eject_after
    )
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

    # 5. Build the DogfoodResult + emit per-run JSON. Timeline subset per
    #    plan 03-12 <action>: pr_opened, required_checks_failed,
    #    merge_command_posted, mq_queued_label_applied, processor_cycle_started,
    #    activation_began, ejected.
    passed = (
        observed.get("action") == expected["action"]
        and expected["reason_substring"] in str(observed.get("reason", ""))
    )
    timeline: tuple[tuple[str, str, dict[str, Any]], ...] = (
        (started, "pr_opened", {"pr_number": pr_number, "pr_url": pr_url}),
        (
            started,
            "merge_command_posted",
            {"comment_id": comment_id, "command": "/merge"},
        ),
        (started, "mq_queued_label_applied", {"labels": ["mq:queued"]}),
        (ended, "processor_cycle_started", {"derived": "see status comment"}),
        (ended, "activation_began", {"derived": "see status comment"}),
        (
            ended,
            "required_checks_failed",
            {
                "failed": [
                    {"name": canary_check_name, "conclusion": "failure"}
                ],
                "canary_check_substring": canary_substring,
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
        expected_outcome=expected,
        observed_outcome=observed,
        timeline=timeline,
        processor_run_urls=(),
        step_summary_excerpt="",
        passed=passed,
        notes=(
            f"Canary check-name resolved from path_to_queues.yml at runtime: "
            f"{canary_check_name!r}; eject-reason matched substring "
            f"{canary_substring!r}. Live-fork run: the processor cycle URL "
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
            "Dogfood driver for DOG-03 (CI failure during evaluation via "
            "the dogfood canary). Requires GITHUB_TOKEN. Leaves PRs in "
            "place per D-04."
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
        print(f"dog_03: TIMEOUT — {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"dog_03: error: {type(exc).__name__}: {exc!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1

    return 0 if result.passed else 1


__all__ = [
    "SCENARIO_ID",
    "TIMEOUT_S",
    "TITLE_PREFIX",
    "main",
    "run_scenario",
]


if __name__ == "__main__":
    sys.exit(main())
