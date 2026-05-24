"""Shared result schema and observations for RFC §4.3.1 audit evidence."""

from __future__ import annotations

import dataclasses
import json
import time
from collections.abc import Callable
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


__all__ = [
    "AuditResult",
    "activation_status_observation",
    "audit_comments_matching",
    "emit_result",
    "poll_pr_state",
    "remaining_mq_labels",
    "workflow_run_urls",
]
