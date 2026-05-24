"""Shared result schema and observations for RFC §4.3.1 audit evidence."""

from __future__ import annotations

import base64
import os
import secrets
import subprocess
from datetime import UTC, datetime
import dataclasses
import json
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_DEFAULT_OUTPUT_DIR = Path(".planning/phases/04A-audit-validator/audit-runs")
_STATUS_MARKER = "<!-- rocm-mq-status -->"


@dataclass(frozen=True, slots=True)
class AuditResult:
    """Per-scenario result emitted by an audit evidence driver.

    This is the stable on-disk JSON contract for the Phase 04A audit evidence
    pack.  Keep the field set pinned: Plan 06's aggregator reads these keys
    directly when producing ``AUDIT-RESULTS.md``.
    """

    scenario_id: str
    run_started_at: str
    run_ended_at: str
    pr_number: int
    pr_url: str
    expected_outcome: dict[str, Any]
    observed_outcome: dict[str, Any]
    timeline: tuple[tuple[str, str, dict[str, Any]], ...]
    audit_run_urls: tuple[str, ...]
    passed: bool
    notes: str
    source_path: str


def emit_result(result: AuditResult, *, output_dir: Path | None = None) -> Path:
    """Write ``result`` as deterministic JSON and return the created path."""

    target_dir = output_dir if output_dir is not None else _DEFAULT_OUTPUT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)

    safe_ts = result.run_started_at.replace(":", "-")
    path = target_dir / f"{safe_ts}-{result.scenario_id}.json"
    path.write_text(
        json.dumps(dataclasses.asdict(result), indent=2, sort_keys=True, default=str),
        encoding="utf-8",
    )
    return path


def poll_pr_state(
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    predicate: Callable[[Any, str, str, int], Any],
    timeout_s: float,
    interval_s: float = 15.0,
) -> Any:
    """Poll ``predicate`` until it returns a truthy value or the timeout elapses."""

    start = time.monotonic()
    while True:
        observed = predicate(client, owner, repo, pr_number)
        if observed:
            return observed
        elapsed = time.monotonic() - start
        if elapsed >= timeout_s:
            raise TimeoutError(
                f"poll_pr_state timed out after {elapsed:.1f}s "
                f"(budget {timeout_s:.1f}s) waiting on PR #{pr_number} in "
                f"{owner}/{repo}"
            )
        time.sleep(interval_s)


def remaining_mq_labels(
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    label_prefix: str = "mq:",
) -> tuple[str, ...]:
    """Return current PR labels in the reserved merge-queue namespace."""

    resp = client.rest.pulls.get(owner, repo, pr_number)
    labels = getattr(resp.parsed_data, "labels", ()) or ()
    return tuple(
        sorted(
            str(getattr(label, "name", ""))
            for label in labels
            if str(getattr(label, "name", "")).startswith(label_prefix)
        )
    )


def audit_comments_matching(
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    family: str,
    detail: str,
    actor: str,
) -> tuple[str, ...]:
    """Return audit explanatory comments containing the required AUDIT-04 fields."""

    resp = client.rest.issues.list_comments(owner, repo, pr_number)
    wanted_actor = actor if actor.startswith("@") else f"@{actor}"
    matches: list[str] = []
    for comment in getattr(resp, "parsed_data", ()) or ():
        body = str(getattr(comment, "body", ""))
        if _STATUS_MARKER in body:
            continue
        if (
            family in body
            and detail in body
            and wanted_actor in body
            and "/merge" in body
        ):
            matches.append(body)
    return tuple(matches)


def activation_status_observation(
    client: Any,
    owner: str,
    repo: str,
    head_sha: str,
    *,
    context: str = "merge-queue/active",
) -> dict[str, str] | None:
    """Return the observed activation status state/context for ``head_sha``."""

    resp = client.rest.repos.list_commit_statuses_for_ref(owner, repo, head_sha)
    for status in getattr(resp, "parsed_data", ()) or ():
        observed_context = str(getattr(status, "context", ""))
        if observed_context == context:
            return {
                "context": observed_context,
                "state": str(getattr(status, "state", "")),
            }
    return None


def workflow_run_urls(
    client: Any,
    owner: str,
    repo: str,
    *,
    pr_number: int,
) -> tuple[str, ...]:
    """Return recent audit workflow run URLs when the client exposes Actions APIs."""

    del pr_number  # Actions list endpoints are repo-scoped; keep the API future-proof.
    actions = getattr(getattr(client, "rest", None), "actions", None)
    if actions is None or not hasattr(actions, "list_workflow_runs"):
        return ()
    try:
        resp = actions.list_workflow_runs(owner, repo, event="pull_request_target")
    except Exception:
        return ()
    parsed = getattr(resp, "parsed_data", None)
    runs = getattr(parsed, "workflow_runs", ()) or ()
    return tuple(
        str(url)
        for run in runs
        if (url := getattr(run, "html_url", None)) is not None
    )


def audit_eject_observation(
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    head_sha: str,
    expected: dict[str, Any],
    actor: str,
    activation_context: str = "merge-queue/active",
) -> dict[str, Any]:
    """Collect the concrete state required for an audit-evidence pass."""

    labels = remaining_mq_labels(client, owner, repo, pr_number)
    status = activation_status_observation(
        client, owner, repo, head_sha, context=activation_context
    )
    comments = audit_comments_matching(
        client,
        owner,
        repo,
        pr_number,
        family=str(expected["family"]),
        detail=str(expected["detail"]),
        actor=actor,
    )
    return {
        "remaining_mq_labels": list(labels),
        "activation_status": status,
        "audit_comment_count": len(comments),
        "audit_comments": list(comments),
        "passed": (
            len(labels) == 0
            and status == {"context": activation_context, "state": "error"}
            and len(comments) > 0
        ),
    }


def audit_eject_predicate(
    client: Any,
    owner: str,
    repo: str,
    pr_number: int,
    *,
    head_sha: str,
    expected: dict[str, Any],
    actor: str,
    inject_observed_after: Callable[[int], None] | None = None,
) -> dict[str, Any] | None:
    """Predicate used by drivers: truthy only after all audit cleanup is observed."""

    if inject_observed_after is not None:
        inject_observed_after(pr_number)
    observed = audit_eject_observation(
        client,
        owner,
        repo,
        pr_number,
        head_sha=head_sha,
        expected=expected,
        actor=actor,
    )
    return observed if observed["passed"] else None


def create_audit_pr(
    client: Any,
    owner: str,
    repo: str,
    *,
    scenario_id: str,
    file_path: str,
    file_content: str,
    title_suffix: str,
) -> tuple[int, str, str]:
    """Open a deterministic audit-evidence PR and return number, URL, head SHA."""

    branch = _create_audit_branch_with_file(
        client,
        owner,
        repo,
        scenario_id=scenario_id,
        file_path=file_path,
        file_content=file_content,
    )
    pr_resp = client.rest.pulls.create(
        owner,
        repo,
        title=f"[audit {scenario_id}] {title_suffix}",
        head=branch,
        base="develop",
        body=(
            f"Auto-generated by `rocm_mq.audit_evidence` for `{scenario_id}`. "
            "This PR is part of the Phase 04A audit evidence pack."
        ),
        maintainer_can_modify=True,
    )
    pr = pr_resp.parsed_data
    live = client.rest.pulls.get(owner, repo, int(pr.number)).parsed_data
    return int(pr.number), str(pr.html_url), str(getattr(live.head, "sha", ""))


def create_labeled_issue_pr(
    client: Any,
    owner: str,
    repo: str,
    *,
    scenario_id: str,
    file_path: str,
    file_content: str,
    labels: Sequence[str],
) -> tuple[int, str, str]:
    """Create an issue with mq labels, then convert it into a PR.

    GitHub's PR create endpoint cannot set labels directly, but it can convert
    an existing issue. Creating the issue with labels first arranges for the
    resulting PR's opened payload to carry labels, exercising the labels-at-open
    row instead of the post-open label-add row.
    """

    branch = _create_audit_branch_with_file(
        client,
        owner,
        repo,
        scenario_id=scenario_id,
        file_path=file_path,
        file_content=file_content,
    )
    issue_resp = client.rest.issues.create(
        owner,
        repo,
        title=f"[audit {scenario_id}] labels-at-open",
        body=(
            f"Auto-generated seed issue for `{scenario_id}`. It is converted "
            "to a PR so the opened event contains stale mq:* labels."
        ),
        labels=list(labels),
    )
    issue = issue_resp.parsed_data
    pr_resp = client.rest.pulls.create(
        owner,
        repo,
        issue=int(issue.number),
        head=branch,
        base="develop",
        maintainer_can_modify=True,
    )
    pr = pr_resp.parsed_data
    live = client.rest.pulls.get(owner, repo, int(pr.number)).parsed_data
    return int(pr.number), str(pr.html_url), str(getattr(live.head, "sha", ""))


def run_audit_scenario(
    client: Any,
    *,
    owner: str,
    repo: str,
    scenario_id: str,
    expected: dict[str, Any],
    source_path: str,
    output_dir: Path | None,
    poll_interval_s: float,
    poll_timeout_s: float,
    actor: str,
    prepare_pr: Callable[[Any, str, str], tuple[int, str, str]],
    tamper: Callable[[Any, str, str, int], dict[str, Any]],
    inject_observed_after: Callable[[int], None] | None = None,
) -> AuditResult:
    """Run one audit scenario and emit the JSON result, including failures."""

    started = datetime.now(tz=UTC).isoformat()
    pr_number, pr_url, head_sha = prepare_pr(client, owner, repo)
    tamper_observation = tamper(client, owner, repo, pr_number)

    notes = ""
    try:
        observed = poll_pr_state(
            client,
            owner,
            repo,
            pr_number,
            predicate=lambda c, o, r, n: audit_eject_predicate(
                c,
                o,
                r,
                n,
                head_sha=head_sha,
                expected=expected,
                actor=actor,
                inject_observed_after=inject_observed_after,
            ),
            timeout_s=poll_timeout_s,
            interval_s=poll_interval_s,
        )
    except TimeoutError as exc:
        observed = audit_eject_observation(
            client,
            owner,
            repo,
            pr_number,
            head_sha=head_sha,
            expected=expected,
            actor=actor,
        )
        notes = f"timeout waiting for audit cleanup: {exc}"

    ended = datetime.now(tz=UTC).isoformat()
    timeline: tuple[tuple[str, str, dict[str, Any]], ...] = (
        (started, "pr_prepared", {"pr_number": pr_number, "pr_url": pr_url}),
        (started, "tamper_applied", tamper_observation),
        (ended, "audit_observed", observed),
    )
    result = AuditResult(
        scenario_id=scenario_id,
        run_started_at=started,
        run_ended_at=ended,
        pr_number=pr_number,
        pr_url=pr_url,
        expected_outcome=expected,
        observed_outcome=observed,
        timeline=timeline,
        audit_run_urls=workflow_run_urls(client, owner, repo, pr_number=pr_number),
        passed=bool(observed["passed"]),
        notes=notes,
        source_path=source_path,
    )
    emit_result(result, output_dir=output_dir)
    return result


def run_driver_cli(
    argv: list[str] | None,
    *,
    scenario_id: str,
    expected: dict[str, Any],
    run_scenario: Callable[..., AuditResult],
    build_client: Callable[[str], Any],
) -> int:
    """Shared CLI wrapper for live audit-evidence modules."""

    import argparse
    import sys
    import traceback

    parser = argparse.ArgumentParser(
        prog=f"rocm-mq-{scenario_id}",
        description=(
            f"Audit evidence driver for {scenario_id} "
            f"({expected['family']}: {expected['detail']}). Requires GITHUB_TOKEN."
        ),
    )
    parser.add_argument("--owner", required=True)
    parser.add_argument("--repo", required=True)
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print(
            "error: GITHUB_TOKEN environment variable is not set; this driver "
            "requires a token capable of mutating fork PR labels/lifecycle.",
            file=sys.stderr,
        )
        return 2

    try:
        result = run_scenario(build_client(token), owner=args.owner, repo=args.repo)
    except Exception as exc:
        print(f"{scenario_id}: error: {type(exc).__name__}: {exc!r}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return 1
    return 0 if result.passed else 1


def post_merge_command(client: Any, owner: str, repo: str, pr_number: int) -> int:
    """Post `/merge` to seed queued labels through the real handler path."""

    resp = client.rest.issues.create_comment(owner, repo, pr_number, body="/merge")
    return int(resp.parsed_data.id)


def run_gh_pr_ready_undo(owner: str, repo: str, pr_number: int) -> dict[str, Any]:
    """Convert a PR to draft with gh CLI; REST has no equivalent endpoint."""

    cmd = [
        "gh",
        "pr",
        "ready",
        "--undo",
        str(pr_number),
        "--repo",
        f"{owner}/{repo}",
    ]
    completed = subprocess.run(cmd, check=True, capture_output=True, text=True)
    return {
        "operation": "converted_to_draft",
        "command": " ".join(cmd),
        "stdout": completed.stdout,
        "stderr": completed.stderr,
    }


def _create_audit_branch_with_file(
    client: Any,
    owner: str,
    repo: str,
    *,
    scenario_id: str,
    file_path: str,
    file_content: str,
) -> str:
    branch = f"audit/{scenario_id}-{secrets.token_hex(4)}"
    ref_resp = client.rest.git.get_ref(owner, repo, "heads/develop")
    ref_obj = ref_resp.parsed_data
    target = getattr(ref_obj, "object_", None) or ref_obj.object
    develop_tip = str(target.sha)
    client.rest.git.create_ref(owner, repo, ref=f"refs/heads/{branch}", sha=develop_tip)
    encoded = base64.b64encode(file_content.encode("utf-8")).decode("ascii")
    client.rest.repos.create_or_update_file_contents(
        owner,
        repo,
        file_path,
        message=f"[audit {scenario_id}] seed {file_path}",
        content=encoded,
        branch=branch,
    )
    return branch


__all__ = [
    "AuditResult",
    "activation_status_observation",
    "audit_comments_matching",
    "audit_eject_observation",
    "audit_eject_predicate",
    "create_audit_pr",
    "create_labeled_issue_pr",
    "emit_result",
    "poll_pr_state",
    "post_merge_command",
    "remaining_mq_labels",
    "run_audit_scenario",
    "run_driver_cli",
    "run_gh_pr_ready_undo",
    "workflow_run_urls",
]
