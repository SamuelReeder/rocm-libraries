"""Scripted audit evidence for post-open mq:* label addition tampering."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from rocm_mq.audit_evidence._base import (
    AuditResult,
    create_audit_pr,
    run_audit_scenario,
    run_driver_cli,
)

SCENARIO_ID = "audit_label_added"
EXPECTED: dict[str, Any] = {
    "action": "Eject",
    "family": "label tamper",
    "detail": "post-open-add",
    "remaining_mq_labels": 0,
    "activation_state": "error",
    "comment_instruction": "/merge",
}
TIMEOUT_S = 60

_FILE_PATH = "projects/hipdnn/audit-evidence-label-added.txt"
_FILE_CONTENT = "audit evidence: post-open mq label add\n"
_MQ_LABELS = ("mq:queued", "mq:hipdnn")


def _build_client(token: str) -> Any:
    from rocm_mq.gh import GitHubClient

    return GitHubClient(token=token)


def _prepare_pr(client: Any, owner: str, repo: str) -> tuple[int, str, str]:
    return create_audit_pr(
        client,
        owner,
        repo,
        scenario_id=SCENARIO_ID,
        file_path=_FILE_PATH,
        file_content=_FILE_CONTENT,
        title_suffix="post-open-label-add",
    )


def _tamper(client: Any, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
    client.rest.issues.add_labels(owner, repo, pr_number, data={"labels": list(_MQ_LABELS)})
    return {"operation": "add_labels", "labels": list(_MQ_LABELS)}


def run_scenario(
    client: Any,
    *,
    owner: str,
    repo: str,
    output_dir: Path | None = None,
    poll_interval_s: float = 5.0,
    poll_timeout_s: float | None = None,
    actor: str = "alice",
    prepare_pr: Callable[[Any, str, str], tuple[int, str, str]] | None = None,
    tamper: Callable[[Any, str, str, int], dict[str, Any]] | None = None,
    inject_observed_after: Callable[[int], None] | None = None,
) -> AuditResult:
    return run_audit_scenario(
        client,
        owner=owner,
        repo=repo,
        scenario_id=SCENARIO_ID,
        expected=EXPECTED,
        source_path=__name__,
        output_dir=output_dir,
        poll_interval_s=poll_interval_s,
        poll_timeout_s=TIMEOUT_S if poll_timeout_s is None else poll_timeout_s,
        actor=actor,
        prepare_pr=_prepare_pr if prepare_pr is None else prepare_pr,
        tamper=_tamper if tamper is None else tamper,
        inject_observed_after=inject_observed_after,
    )


def main(argv: list[str] | None = None) -> int:
    return run_driver_cli(
        argv,
        scenario_id=SCENARIO_ID,
        expected=EXPECTED,
        run_scenario=run_scenario,
        build_client=_build_client,
    )


__all__ = ["EXPECTED", "SCENARIO_ID", "TIMEOUT_S", "main", "run_scenario"]


if __name__ == "__main__":
    raise SystemExit(main())
