"""Scripted audit evidence for changing an enqueued PR base away from develop."""

from __future__ import annotations

import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any

from rocm_mq.audit_evidence._base import (
    AuditResult,
    create_audit_pr,
    post_merge_command,
    wait_for_mq_labels,
    run_audit_scenario,
    run_driver_cli,
)

SCENARIO_ID = "audit_base_ref_changed"
EXPECTED: dict[str, Any] = {
    "action": "Eject",
    "family": "lifecycle invalidation",
    "detail": "base-ref-change",
    "remaining_mq_labels": 0,
    "activation_state": "error",
    "comment_instruction": "/merge",
}
TIMEOUT_S = 60

_FILE_PATH = "projects/hipdnn/audit-evidence-base-ref-change.txt"
_FILE_CONTENT = "audit evidence: base ref change\n"


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
        title_suffix="base-ref-change",
    )


def _tamper(client: Any, owner: str, repo: str, pr_number: int) -> dict[str, Any]:
    command_id = post_merge_command(client, owner, repo, pr_number)
    labels_before = wait_for_mq_labels(client, owner, repo, pr_number)
    ref_resp = client.rest.git.get_ref(owner, repo, "heads/develop")
    ref_obj = ref_resp.parsed_data
    target = getattr(ref_obj, "object_", None) or ref_obj.object
    base_branch = f"audit-base/{pr_number}-{secrets.token_hex(4)}"
    client.rest.git.create_ref(
        owner, repo, ref=f"refs/heads/{base_branch}", sha=str(target.sha)
    )
    client.rest.pulls.update(owner, repo, pr_number, base=base_branch)
    return {
        "operation": "change_base",
        "base": base_branch,
        "labels_before_tamper": list(labels_before),
        "merge_command_comment_id": command_id,
    }


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
