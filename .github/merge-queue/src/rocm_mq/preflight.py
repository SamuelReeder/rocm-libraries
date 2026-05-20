"""rocm_mq.preflight — Workflow startup pre-flight check (WF-10).

Runs as the FIRST step of both ``mq-handler.yml`` (plan 03-07) and
``mq-processor.yml`` (plan 03-08), BEFORE the App installation token is
minted. This sequencing is load-bearing: if the fork is misconfigured we
never want the workflow to even hold the App installation token (RFC §8
self-bootstrap protection, threat T-03-04-01 in plan 03-04). The check uses
the workflow's default ``GITHUB_TOKEN`` (read-only, ``contents: read``
scope) — never the App token.

Plan 03-04 Task 1 decision (option-a, comprehensive scope):

  Check 1 (WF-10): default branch is ``develop``.
  Check 2 (App-identity slug-match) — DEFERRED to processor startup per
      RESEARCH.md Area #23 nuance. ``apps.get_authenticated`` requires an
      App-token, which preflight does not have. The check surfaces instead
      as the first processor failure with a clear cause (the processor's
      existing ``resolve_app_identity`` invocation inside ``process_cycle``
      raises if the wrong App is installed, satisfying the threat-model
      coverage passively).
  Check 3: ``.github/merge-queue/path_to_queues.yml`` is loadable from the
      ``develop`` ref via the Contents API. Catches a missing-or-misnamed
      config file, mis-scoped GITHUB_TOKEN, branch-protection misconfig, or
      Contents API outage BEFORE the App-token mint.

Failure-mode discipline (CONTEXT.md "Pre-flight failure mode is non-zero
exit + structured stderr + ``$GITHUB_STEP_SUMMARY`` write"):

- Non-zero exit code (1 for check failure; 2 for usage error — mirrors
  ``cmd_process.py`` so the workflow's run-log structure stays uniform).
- Structured stderr line prefixed ``preflight FAILED:`` so an operator
  scanning the workflow run-log finds the cause immediately.
- Append to ``$GITHUB_STEP_SUMMARY`` (if set) in ``"a"`` mode so a prior
  step's content survives (GHA convention).

Public surface (Phase 3 contract):

- ``main(argv: list[str] | None = None) -> int`` — entry point; argv parses
  via ``_parse_args`` (``--repo OWNER/REPO`` only). Returns:
    * 0 — all checks pass
    * 1 — at least one check failed (Check 1 or Check 3)
    * 2 — usage error (missing/malformed ``--repo``; missing ``GITHUB_TOKEN``)

Layering (PURE-09):

This module is I/O layer. It freely imports ``rocm_mq.gh.GitHubClient``
(I/O) and stdlib (``argparse``, ``os``, ``sys``). It intentionally does NOT
import ``rocm_mq.decision`` or any pure-layer module — preflight runs
before the cycle's read→decide→execute layering even begins.
"""

from __future__ import annotations

import argparse
import os
import sys

from rocm_mq.gh import GitHubClient

# ---------------------------------------------------------------------------
# Internal constants
# ---------------------------------------------------------------------------

_EXPECTED_DEFAULT_BRANCH = "develop"
_PATH_TO_QUEUES_FILE = ".github/merge-queue/path_to_queues.yml"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    """Parse argv for the preflight CLI.

    Mirrors the ``cmd_process.py`` ``--repo`` convention: positional is a
    repo in OWNER/REPO form; default falls back to ``$GITHUB_REPOSITORY``
    which GHA runners set automatically.
    """
    parser = argparse.ArgumentParser(
        prog="rocm-mq preflight",
        description=(
            "Workflow startup pre-flight check (WF-10). Runs BEFORE the App "
            "installation token is minted; uses the workflow's default "
            "GITHUB_TOKEN (read-only). Exits 0 if the fork's default branch "
            f"is {_EXPECTED_DEFAULT_BRANCH!r} AND "
            f"{_PATH_TO_QUEUES_FILE} is loadable from the develop ref."
        ),
    )
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", ""),
        help=(
            "GitHub repository in OWNER/REPO form. Defaults to "
            "$GITHUB_REPOSITORY (set automatically in GHA runners)."
        ),
    )
    return parser.parse_args(argv)


def _fail(message: str) -> None:
    """Emit a structured ``preflight FAILED:`` line and append to $GITHUB_STEP_SUMMARY.

    Mirrors the ``cmd_process.py`` lines 168-176 summary-write pattern:
    open in ``"a"`` (append) mode so a prior step's content survives, and
    ensure the appended block ends with a newline.

    The summary section header is ``## Pre-flight FAILED`` so an operator
    scanning the rendered GHA summary on a failed run finds the cause
    visually demarcated from the surrounding cycle summary (which uses
    ``## Cycle summary`` per the existing renderer).
    """
    print(f"preflight FAILED: {message}", file=sys.stderr)

    step_summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if step_summary_path:
        section = f"## Pre-flight FAILED\n\n{message}\n"
        with open(step_summary_path, "a", encoding="utf-8") as fh:
            fh.write(section)
            if not section.endswith("\n"):
                fh.write("\n")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run the comprehensive 2-check pre-flight.

    Args:
        argv: CLI argument list (e.g., ``["--repo", "owner/repo"]``). When
            ``None``, ``argparse`` reads ``sys.argv[1:]``.

    Returns:
        Process exit code:
          - 0 on all-checks-pass
          - 1 on Check 1 or Check 3 failure
          - 2 on usage error (--repo missing/malformed or GITHUB_TOKEN unset)

    Validation pattern mirrors ``cmd_process.run_process_cycle`` lines
    321-349 so the exit-code-2 convention is uniform across rocm_mq CLI
    entrypoints — Phase 3 workflow tests rely on the distinction between
    exit-1 (check failed) and exit-2 (operator misconfigured the invocation).
    """
    args = _parse_args(argv)

    # Validate --repo (usage error — exit 2).
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

    # Validate GITHUB_TOKEN presence (usage error — exit 2).
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print(
            "error: GITHUB_TOKEN environment variable is not set; preflight "
            "uses the workflow's default token (read-only) — set "
            "permissions.contents: read on the calling job and pass "
            "GH_TOKEN=${{ github.token }}.",
            file=sys.stderr,
        )
        return 2

    client = GitHubClient(token=token)

    # Check 1 (WF-10): default branch is develop.
    #
    # Threat-model tie-in (T-03-04-01): the fork being on the wrong default
    # branch means the queue would serialize merges into the wrong place —
    # we MUST short-circuit before the App-token is even minted so a
    # misconfigured fork never holds the installation token.
    repo_resp = client.rest.repos.get(owner, repo)
    actual_default = repo_resp.parsed_data.default_branch
    if actual_default != _EXPECTED_DEFAULT_BRANCH:
        _fail(f"default_branch={actual_default!r}, expected {_EXPECTED_DEFAULT_BRANCH!r}")
        return 1

    # Check 3: PATH_TO_QUEUES loadable from develop.
    #
    # We call get_content directly (not load_from_develop) because preflight
    # only cares about FETCH+DECODE, not the YAML parse — a yaml.safe_load
    # error is a separate failure class that load_from_develop's callers
    # already handle. Catching Exception broadly here is intentional: the
    # Contents API may raise githubkit.RequestFailed (404, 403, 500), the
    # underlying httpx may raise transport errors, or base64-decode may
    # fail on a malformed response. All of these are equivalent for
    # preflight's purpose: the file is not loadable; surface the repr to
    # the operator and exit non-zero.
    try:
        client.rest.repos.get_content(
            owner, repo, _PATH_TO_QUEUES_FILE, ref=_EXPECTED_DEFAULT_BRANCH
        )
    except Exception as exc:
        _fail(f"PATH_TO_QUEUES not loadable from develop: {exc!r}")
        return 1

    print("preflight passed", file=sys.stderr)
    return 0


__all__ = ["main"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
